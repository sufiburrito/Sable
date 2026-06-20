#!/usr/bin/env python3
"""
journal/execution_review.py — Sable's advised calls vs the user's ACTUAL fills.

The forward ledger holds Sable's *idealized* BUY calls (advised entry/target/stop); the
`closed_lots` table holds the user's *real* trades (actual buy & sell). This module finally
puts them side by side: for each BUY call, find the user's nearest real buy and the lot it
became, then measure how the real execution compared to the advice — entry slippage, how
many days late, and where they actually sold vs the advised target.

Matching is deliberately loose so trades made *outside* the tight ±7d/±5% "missed" window
are still captured: the nearest user BUY 0–45 days AFTER the call, at ANY price. Each record
carries the slippage and a tier (`on_level` if also within ±7d/±5%, else `loose`) so a fuzzy
link is visible and human-judgeable. This is the dataset Sable reads to coach execution —
it is NOT (yet) fed into the numeric edge model.

Output: data/journal/execution_review.jsonl + a summary to stdout.

Usage:  python3 -m journal.execution_review
"""
import json
from pathlib import Path

import forward_lib as fl
from journal import missed_trades, realized_pnl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "journal" / "execution_review.jsonl"

MATCH_DAYS = 45          # link the nearest user BUY up to this many days AFTER the call
ON_LEVEL_DAYS = missed_trades.WINDOW_DAYS   # ±7d  → the tight "on the level" tier
ON_LEVEL_TOL = missed_trades.PRICE_TOL      # ±5%  → "


def match_call(call: dict, buys: dict) -> dict | None:
    """Best user BUY linked to this call. Returns {buy_date, buy_price, lag_days, tier} or None.

    Two ways a buy qualifies:
      - `on_level`: within ±7d AND ±5% of the advised entry (either direction — includes a buy
        placed just *before* the alert, which the tight 'missed' test also counts as taken);
      - `loose`:    0–45 days AFTER the call, at ANY price (captures late / off-level fills).
    On-level matches win; otherwise the nearest in time. `lag_days` is signed (negative = bought
    before the alert fired)."""
    fired, entry = missed_trades._fired_date(call), call["entry"]
    cands = []
    for d, price in buys.get(call["ticker"].upper(), []):
        lag = (d - fired).days
        on_level = abs(lag) <= ON_LEVEL_DAYS and abs(price - entry) / entry <= ON_LEVEL_TOL
        loose = 0 <= lag <= MATCH_DAYS
        if on_level or loose:
            cands.append((0 if on_level else 1, abs(lag), lag, d, price,
                          "on_level" if on_level else "loose"))
    if not cands:
        return None
    cands.sort()                                     # on-level first, then nearest in time
    _, _, lag, d, price, tier = cands[0]
    return {"buy_date": str(d), "buy_price": round(float(price), 2), "lag_days": lag, "tier": tier}


def _exit_for_buy(ticker: str, buy_date: str, buy_price: float, closed: list[dict]) -> dict:
    """Join a matched buy to the FIFO closed lot(s) it became (a buy can sell in tranches,
    or still be open). Returns the aggregated real exit, or an open/holding marker."""
    lots = [c for c in closed if c["symbol"].upper() == ticker.upper()
            and c["buy_date"] == buy_date and abs(c["buy_price"] - buy_price) <= 0.01]
    if not lots:
        return {"status": "taken_open"}              # bought, not yet sold (still holding)
    qty = sum(c["quantity"] for c in lots)
    vwap_sell = sum(c["sell_price"] * c["quantity"] for c in lots) / qty if qty else None
    pnl = sum(c["realized_pnl"] for c in lots)
    cost = sum(c["buy_price"] * c["quantity"] for c in lots)
    return {
        "status": "taken_closed",
        "user_sell_date": max(c["sell_date"] for c in lots),
        "user_sell_price": round(vwap_sell, 2) if vwap_sell is not None else None,
        "realized_pnl": round(pnl, 2),
        "realized_pct": round(pnl / cost * 100, 2) if cost else None,
        "days_held": max(c["holding_days"] for c in lots),
        "sold_qty": qty,
    }


def _exit_quality(left_on_table_pct: float | None, complete: bool) -> str:
    """Verified verdict from the REAL post-sell move (peak after exit), not the forecast target."""
    if left_on_table_pct is None:
        return "n/a"
    if not complete:
        return "pending"                 # too recent — the window hasn't fully printed yet
    if left_on_table_pct >= 3.0:
        return "early"                   # it really ran higher after you sold
    if left_on_table_pct <= 0.5:
        return "good"                    # you sold within a hair of the actual high
    return "ok"


def build_execution_review(ledger: list[dict], buys: dict, closed: list[dict]) -> list[dict]:
    """One record per BUY call the user actually took (on_level or loose), advice vs reality."""
    out, ohlc = [], {}
    for call in ledger:
        if call.get("alert_type") != "BUY" or not call.get("entry") or call.get("status") == "excluded":
            continue
        m = match_call(call, buys)
        if m is None:
            continue
        entry, target = call["entry"], call.get("target")
        tk = call["ticker"]
        if tk not in ohlc:
            ohlc[tk] = fl.load_ohlc(tk)
        # Was the advised entry actually reachable? (BUY level → did a daily Low touch it?)
        entry_hit, entry_low = fl.first_touch_low(ohlc[tk], call["fired_at"], entry)
        rec = {
            "ticker": call["ticker"], "fired_on": str(missed_trades._fired_date(call)),
            "fired_at": call.get("fired_at"), "tier": m["tier"],
            "advised_entry": round(entry, 2), "advised_target": target,
            "advised_stop": call.get("stop"), "rr": call.get("rr"),
            "advised_realized_R": call.get("realized_R"),
            "advised_exit_reason": call.get("exit_reason"),   # did Sable's target ever print?
            "entry_hit": entry_hit,                            # advised buy level actually reached?
            "entry_closest": entry_low if entry_hit is False else None,  # closest it got, when not
            "regime": call.get("regime_at_fire"),
            "user_buy_date": m["buy_date"], "user_buy_price": m["buy_price"],
            "entry_slippage_pct": round((m["buy_price"] - entry) / entry * 100, 2),
            "lag_days": m["lag_days"],
        }
        ex = _exit_for_buy(call["ticker"], m["buy_date"], m["buy_price"], closed)
        rec.update(ex)
        if ex["status"] == "taken_closed" and target:
            rec["exit_vs_target_pct"] = round((ex["user_sell_price"] - target) / target * 100, 2)
        # VERIFIED exit quality: how much higher the stock ACTUALLY traded after you sold
        if ex["status"] == "taken_closed":
            exc = fl.excursion(ohlc[tk], ex["user_sell_date"], ex["user_sell_price"])
            rec["left_on_table_pct"] = exc["peak_pct"] if exc else None
            rec["drawdown_after_pct"] = exc["trough_pct"] if exc else None
            rec["exit_window_complete"] = exc["complete"] if exc else False
            rec["exit_quality"] = _exit_quality(rec["left_on_table_pct"], rec["exit_window_complete"])
        out.append(rec)
    # one record per (ticker, buy) — a re-fired level shouldn't double-count the same fill
    out.sort(key=lambda r: r["fired_on"])
    seen, deduped = set(), []
    for r in out:
        key = (r["ticker"].upper(), r["user_buy_date"], round(r["user_buy_price"]))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def summarize(recs: list[dict]) -> str:
    on_level = [r for r in recs if r["tier"] == "on_level"]
    loose = [r for r in recs if r["tier"] == "loose"]
    closed = [r for r in recs if r.get("status") == "taken_closed"]
    avg_slip = sum(r["entry_slippage_pct"] for r in recs) / len(recs) if recs else 0.0
    lines = ["🎯 **Sable advice vs your execution** (BUY calls you actually took)", ""]
    lines.append(f"Taken: {len(recs)}  ·  on-level {len(on_level)}  ·  loose {len(loose)}  "
                 f"·  closed {len(closed)}  ·  avg entry slippage {avg_slip:+.1f}%")
    beat = [r for r in closed if r.get("exit_vs_target_pct") is not None and r["exit_vs_target_pct"] >= 0]
    if closed:
        lines.append(f"Sold ABOVE the advised target: {len(beat)}/{len(closed)}")
    return "\n".join(lines)


def main():
    ledger = fl.load_ledger()
    buys = missed_trades.load_user_buys()
    closed, _, _ = realized_pnl.compute_closed_lots(realized_pnl.load_transactions())
    recs = build_execution_review(ledger, buys, closed)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    print(summarize(recs))
    print(f"\nWrote {OUT}  ({len(recs)} taken calls)")


if __name__ == "__main__":
    main()
