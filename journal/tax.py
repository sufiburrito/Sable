#!/usr/bin/env python3
"""
journal/tax.py — Indian capital-gains tax planning from the journal data.

**Planning aid, NOT tax advice.** Current regime (post-23-Jul-2024):
  STCG (≤12mo, listed equity, STT-paid) 20% §111A · LTCG (>12mo) 12.5% §112A
  with a ₹1.25L/FY aggregate exemption (no indexation).
  Set-off: STCL → STCG **and** LTCG · LTCL → LTCG only. FY = 1 Apr → 31 Mar.
Rates/exemption come from charge_model.json (editable).

Pure Python, no LLM, no network (current price = OHLC-cache last close). Computes
realized-FY set-off + tax, loss-harvest candidates, the LTCG-threshold watch, the
exemption tracker, and key-date countdowns. The Obsidian view + nightly build consume it.

Usage:  python3 -m journal.tax
"""
import datetime as dt

import forward_lib as fl
from journal import realized_pnl, pnl_statement

LTCG_DAYS = 365            # >12 months ≈ >365 days
WATCH_WINDOW = 45          # flag STCG lots within this many days of becoming LTCG


def _today() -> dt.date:
    return dt.date.today()


def india_fy(d: dt.date) -> tuple[str, dt.date, dt.date]:
    """Indian financial year (1 Apr → 31 Mar) containing date d."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y}-{str(y + 1)[2:]}", dt.date(y, 4, 1), dt.date(y + 1, 3, 31)


def holdings_with_price(open_lots: list[dict], today: dt.date | None = None) -> list[dict]:
    """Attach last-close price, unrealized P&L, holding days, and STCG/LTCG class."""
    today = today or _today()
    out, cache = [], {}
    for lot in open_lots:
        sym = lot["symbol"]
        if sym not in cache:
            df = fl.load_ohlc(sym)
            cache[sym] = float(df["Close"].iloc[-1]) if (df is not None and len(df)) else None
        cp = cache[sym]
        try:
            bd = dt.date.fromisoformat(lot["buy_date"])
        except (ValueError, TypeError):
            continue
        if cp is None or not lot.get("buy_price"):
            continue
        qty, bp = lot["quantity"], lot["buy_price"]
        days = (today - bd).days
        out.append({
            "symbol": sym, "quantity": qty, "buy_date": lot["buy_date"], "buy_price": bp,
            "price": round(cp, 2), "cost": round(qty * bp, 2), "value": round(qty * cp, 2),
            "unrealized": round(qty * (cp - bp), 2), "unrealized_pct": round((cp - bp) / bp * 100, 2),
            "holding_days": days, "gain_class": "LTCG" if days > LTCG_DAYS else "STCG",
        })
    return out


def fy_realized(closed: list[dict], fy_start: dt.date, fy_end: dt.date) -> dict:
    """Realized STCG/LTCG gains and STCL/LTCL losses for sells within the FY."""
    s, e = fy_start.isoformat(), fy_end.isoformat()
    stcg = ltcg = stcl = ltcl = 0.0
    for c in closed:
        if not (s <= c["sell_date"] <= e):
            continue
        p = c["realized_pnl"]
        if c["gain_type"] == "LTCG":
            ltcg += p if p >= 0 else 0; ltcl += -p if p < 0 else 0
        else:
            stcg += p if p >= 0 else 0; stcl += -p if p < 0 else 0
    return {"stcg": round(stcg, 2), "ltcg": round(ltcg, 2),
            "stcl": round(stcl, 2), "ltcl": round(ltcl, 2)}


def compute_tax(realized: dict, model: dict) -> dict:
    """Apply the set-off rules + ₹1.25L LTCG exemption → estimated tax."""
    stcg, ltcg, stcl, ltcl = (realized[k] for k in ("stcg", "ltcg", "stcl", "ltcl"))
    exemption = model.get("ltcg_exemption", 125000)
    net_stcg = max(0.0, stcg - stcl)                     # STCL offsets STCG first…
    stcl_left = max(0.0, stcl - stcg)                    # …then spills into LTCG
    net_ltcg = max(0.0, ltcg - ltcl - stcl_left)         # LTCL offsets only LTCG
    taxable_ltcg = max(0.0, net_ltcg - exemption)
    tax_stcg = net_stcg * model["stcg_rate"]
    tax_ltcg = taxable_ltcg * model["ltcg_rate"]
    return {
        "net_stcg": round(net_stcg, 2), "net_ltcg": round(net_ltcg, 2),
        "exemption": exemption, "exemption_used": round(min(net_ltcg, exemption), 2),
        "exemption_left": round(max(0.0, exemption - net_ltcg), 2),
        "taxable_ltcg": round(taxable_ltcg, 2), "tax_stcg": round(tax_stcg, 2),
        "tax_ltcg": round(tax_ltcg, 2), "total_tax": round(tax_stcg + tax_ltcg, 2),
    }


def apply_carryforward(realized: dict, model: dict,
                       cf_stcl: float = 0.0, cf_ltcl: float = 0.0) -> dict:
    """Tax for one FY after current-year set-off AND brought-forward losses, then exemption.

    Order the tax code uses: current-year losses set off first, then losses carried forward
    from prior years. Within each step the *restricted* loss (LTCL — can only touch LTCG) is
    applied before the *flexible* loss (STCL — STCG then LTCG), so capacity isn't wasted.

    `cf_stcl` / `cf_ltcl` are the loss buckets entering this FY. Returns the tax plus the
    buckets leaving it: brought-forward leftovers PLUS this year's own unabsorbed losses.
    """
    stcg, ltcg, stcl, ltcl = (realized[k] for k in ("stcg", "ltcg", "stcl", "ltcl"))

    # 1) current-year set-off (LTCL on LTCG first, then STCL on STCG then the LTCG remainder)
    net_stcg = max(0.0, stcg - stcl)
    stcl_rem = max(0.0, stcl - stcg)                 # STCL left after eating STCG
    ltcg_a = max(0.0, ltcg - ltcl)                   # LTCG left after LTCL
    cur_ltcl_carry = max(0.0, ltcl - ltcg)           # this year's unabsorbed LTCL
    net_ltcg = max(0.0, ltcg_a - stcl_rem)
    cur_stcl_carry = max(0.0, stcl_rem - ltcg_a)     # this year's unabsorbed STCL

    # 2) brought-forward losses against what current-year set-off left (restricted first again)
    ltcg_b = max(0.0, net_ltcg - cf_ltcl)
    cf_ltcl_out = max(0.0, cf_ltcl - net_ltcg)
    net_stcg2 = max(0.0, net_stcg - cf_stcl)
    cf_stcl_mid = max(0.0, cf_stcl - net_stcg)       # b/f STCL left after current STCG
    net_ltcg2 = max(0.0, ltcg_b - cf_stcl_mid)
    cf_stcl_out = max(0.0, cf_stcl_mid - ltcg_b)

    cf_used = (cf_stcl - cf_stcl_out) + (cf_ltcl - cf_ltcl_out)

    # 3) exemption + tax on what survives
    exemption = model.get("ltcg_exemption", 125000)
    taxable_ltcg = max(0.0, net_ltcg2 - exemption)
    tax = net_stcg2 * model["stcg_rate"] + taxable_ltcg * model["ltcg_rate"]
    return {
        "net_stcg": round(net_stcg2, 2), "net_ltcg": round(net_ltcg2, 2),
        "taxable_ltcg": round(taxable_ltcg, 2), "total_tax": round(tax, 2),
        "exemption_used": round(min(net_ltcg2, exemption), 2),
        "exemption_left": round(max(0.0, exemption - net_ltcg2), 2),
        "cf_used": round(cf_used, 2),
        "cf_stcl_out": round(cf_stcl_out + cur_stcl_carry, 2),   # leftovers + this year's own
        "cf_ltcl_out": round(cf_ltcl_out + cur_ltcl_carry, 2),
        "cur_stcl_carry": round(cur_stcl_carry, 2), "cur_ltcl_carry": round(cur_ltcl_carry, 2),
    }


def fy_effective_series(closed: list[dict], model: dict) -> list[dict]:
    """Per-FY effective (post-charges, post-tax) P&L, oldest→newest, threading carry-forward.

    Each FY carries: gross (Σ realized), charges (Σ per-lot from the charge model), after_charges,
    realized STCG/LTCG/STCL/LTCL, standalone tax (no c/f), tax after carry-forward, both take-home
    figures, effective tax-rate, and the loss buckets flowing in/out."""
    from collections import defaultdict
    by_fy: dict[str, list] = defaultdict(list)
    for c in closed:
        try:
            d = dt.date.fromisoformat(c["sell_date"])
        except (ValueError, TypeError):
            continue
        by_fy[india_fy(d)[0]].append(c)

    cf_stcl = cf_ltcl = 0.0
    out = []
    for fy in sorted(by_fy):                          # chronological so carry-forward accumulates
        lots = by_fy[fy]
        fys, fye = india_fy(dt.date.fromisoformat(lots[0]["sell_date"]))[1:]
        realized = fy_realized(lots, fys, fye)
        gross = round(sum(l["realized_pnl"] for l in lots), 2)
        charges = round(sum(pnl_statement.effective_for_lot(l, model)["charges"] for l in lots), 2)
        after = round(gross - charges, 2)
        standalone = compute_tax(realized, model)
        cf = apply_carryforward(realized, model, cf_stcl, cf_ltcl)
        out.append({
            "fy": fy, "fy_start": fys.isoformat(), "fy_end": fye.isoformat(),
            "n_lots": len(lots), "gross": gross, "charges": charges, "after_charges": after,
            "realized": realized, "tax_standalone": standalone, "tax_after_cf": cf,
            "net_takehome": round(after - standalone["total_tax"], 2),
            "net_takehome_cf": round(after - cf["total_tax"], 2),
            "eff_rate": round(standalone["total_tax"] / gross * 100, 2) if gross > 0 else None,
            "cf_in": {"stcl": round(cf_stcl, 2), "ltcl": round(cf_ltcl, 2)},
            "cf_out": {"stcl": cf["cf_stcl_out"], "ltcl": cf["cf_ltcl_out"]},
            "net_loss": gross < 0,
        })
        cf_stcl, cf_ltcl = cf["cf_stcl_out"], cf["cf_ltcl_out"]
    out.reverse()                                     # newest first for display
    return out


def harvest_candidates(holdings: list[dict], model: dict) -> list[dict]:
    """Underwater holdings whose booked loss could offset gains, ranked by tax saved.
    STCL offsets STCG+LTCG; LTCL offsets only LTCG."""
    out = []
    for h in holdings:
        if h["unrealized"] >= 0:
            continue
        loss = -h["unrealized"]
        cls = "STCL" if h["gain_class"] == "STCG" else "LTCL"
        rate = model["stcg_rate"] if cls == "STCL" else model["ltcg_rate"]
        out.append({**h, "loss_class": cls, "harvestable_loss": round(loss, 2),
                    "max_tax_offset": round(loss * rate, 2)})
    out.sort(key=lambda x: -x["max_tax_offset"])
    return out


def ltcg_threshold_watch(holdings: list[dict], model: dict,
                         today: dt.date | None = None, window: int = WATCH_WINDOW) -> list[dict]:
    """Profitable STCG lots within `window` days of crossing 12mo → tax saved by waiting."""
    today = today or _today()
    out = []
    for h in holdings:
        if h["gain_class"] != "STCG" or h["unrealized"] <= 0:
            continue
        days_to = LTCG_DAYS - h["holding_days"]
        if 0 < days_to <= window:
            saving = h["unrealized"] * (model["stcg_rate"] - model["ltcg_rate"])
            out.append({"symbol": h["symbol"], "holding_days": h["holding_days"],
                        "days_to_ltcg": days_to,
                        "ltcg_date": (today + dt.timedelta(days=days_to)).isoformat(),
                        "unrealized": round(h["unrealized"], 2), "tax_saving": round(saving, 2)})
    out.sort(key=lambda x: x["days_to_ltcg"])
    return out


def key_dates(today: dt.date | None = None) -> dict:
    """Countdown to the current-FY harvest deadline + FY-end, the next advance-tax
    installment, and the next ITR-filing deadline.

    Each date is anchored to *where in the year we are* (computed, never hardcoded):
      - harvest / FY-end belong to the CURRENT FY (so in Jun-2026 they fall in Mar-2027);
      - advance tax is the next 15-Jun/Sep/Dec/Mar installment on or after today;
      - ITR is the next upcoming 31 July — which files the *most recently completed* FY
        (so in Jun-2026 that is 31-Jul-2026 for FY2025-26, NOT next year's return).
    """
    today = today or _today()
    fy, _, fy_end = india_fy(today)
    harvest = dt.date(fy_end.year, 3, 28)                # ~T+1 cushion before 31 Mar
    adv = [dt.date(today.year, m, 15) for m in (6, 9, 12)] + [dt.date(fy_end.year, 3, 15)]
    nxt = min((d for d in adv if d >= today), default=None)
    itr = dt.date(today.year, 7, 31)                     # next 31 July on/after today
    if itr < today:
        itr = dt.date(today.year + 1, 7, 31)
    itr_fy = f"FY{itr.year - 1}-{str(itr.year)[2:]}"     # the FY this return files
    def days(d): return (d - today).days if d else None
    return {"fy": fy, "fy_end": fy_end.isoformat(), "days_to_fy_end": days(fy_end),
            "harvest_by": harvest.isoformat(), "days_to_harvest": days(harvest),
            "next_advance_tax": nxt.isoformat() if nxt else None, "days_to_advance_tax": days(nxt),
            "itr_due": itr.isoformat(), "days_to_itr": days(itr), "itr_fy": itr_fy}


def build_tax_data(today: dt.date | None = None) -> dict:
    today = today or _today()
    model = pnl_statement.load_model()
    closed, open_lots, _ = realized_pnl.compute_closed_lots(realized_pnl.load_transactions())
    fy, fy_start, fy_end = india_fy(today)
    realized = fy_realized(closed, fy_start, fy_end)
    holdings = holdings_with_price(open_lots, today)
    return {
        "fy": fy, "realized": realized, "tax": compute_tax(realized, model),
        "harvest": harvest_candidates(holdings, model),
        "ltcg_watch": ltcg_threshold_watch(holdings, model, today),
        "key_dates": key_dates(today), "model": model, "n_holdings": len(holdings),
    }


def main():
    import json
    d = build_tax_data()
    print(json.dumps({k: d[k] for k in ("fy", "realized", "tax", "key_dates", "n_holdings")},
                     indent=2, default=str))
    print(f"harvest candidates: {len(d['harvest'])} · LTCG-threshold watch: {len(d['ltcg_watch'])}")


if __name__ == "__main__":
    main()
