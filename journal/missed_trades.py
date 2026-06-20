#!/usr/bin/env python3
"""
journal/missed_trades.py — Sable's advised-but-not-taken calls, scored forward.

Cross-references Sable's resolved swing calls (data/forward_ledger.jsonl, from the
forward-test rig) against the user's actual buys (portfolio.db.transactions) to ask,
for every BUY Sable advised: did you take it — and if not, what happened?

  MISSED WINNER : not taken, resolved profitably (R>0) → opportunity cost
  DODGED LOSER  : not taken, resolved at a loss (R<0) → skipping was RIGHT
  PENDING       : not taken, not yet resolved

It's a deliberately two-sided scorecard — skipping a call isn't always a mistake, so
the roll-up nets the missed gains against the dodged losses (= what *following every
missed call* would actually have returned).

Swing timeframe only (the ledger is daily). Intraday → bean algotrading-k95u.
Output: data/journal/missed_trades.jsonl + a Discord-postable summary to stdout.

Usage:  python3 -m journal.missed_trades
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import forward_lib as fl

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "portfolio.db"
OUT = ROOT / "data" / "journal" / "missed_trades.jsonl"

WINDOW_DAYS = 7      # a user BUY within ±7 calendar days of the call counts as "taking" it
PRICE_TOL = 0.05     # …and within ±5% of the alert level (same trade, not a later add)


def _fired_date(call: dict):
    return datetime.fromisoformat(call["fired_at"]).date()


def load_user_buys(db_path: Path = DB) -> dict:
    """symbol(upper) → list of (date, price) for every actual BUY transaction."""
    con = sqlite3.connect(str(db_path))
    out: dict = {}
    for sym, price, ts in con.execute(
        "SELECT symbol, price_per_share, executed_at FROM transactions WHERE trade_type='BUY'"
    ):
        try:
            d = datetime.fromisoformat(str(ts)).date()
        except ValueError:
            continue
        out.setdefault(str(sym).upper(), []).append((d, float(price)))
    con.close()
    return out


def was_taken(call: dict, buys: dict) -> bool:
    """True if the user bought this symbol near the alert level, near the alert date."""
    fired, entry = _fired_date(call), call["entry"]
    for d, price in buys.get(call["ticker"].upper(), []):
        if abs((d - fired).days) <= WINDOW_DAYS and abs(price - entry) / entry <= PRICE_TOL:
            return True
    return False


def classify(call: dict) -> tuple[str, float | None]:
    """(label, counterfactual_pct) from the call's already-resolved outcome.
    counterfactual % = realized_R × risk%  (R expressed back in price-return terms)."""
    r = call.get("realized_R")
    if call["status"] == "open" or r is None:
        return "pending", None
    entry, stop = call["entry"], call.get("stop")
    risk = (entry - stop) / entry if (stop and stop < entry) else None
    pct = round(r * risk * 100, 2) if risk is not None else None
    return ("missed_winner" if r > 0 else "dodged_loser" if r < 0 else "flat"), pct


def _corroboration(call: dict, label: str) -> str:
    """Ground the verdict in what price actually did. `target_hit` = the advised target
    genuinely printed (a real missed winner); `stopped` = the stop really hit (a real dodged
    loss); `soft` = time-cap close, the target NEVER printed (a *theoretical* outcome);
    `pending` = not yet resolved."""
    if label == "pending":
        return "pending"
    return {"target": "target_hit", "stop": "stopped"}.get(call.get("exit_reason") or "", "soft")


def build_missed(ledger: list[dict], buys: dict) -> list[dict]:
    # "Taken" = the tight on-level match OR a looser real fill within 45d (any price);
    # a loosely-taken call belongs in the execution review, not the missed list. Import
    # locally to avoid a circular import (execution_review imports this module).
    from journal.execution_review import match_call
    out, ohlc = [], {}
    for call in ledger:
        if call.get("alert_type") != "BUY" or not call.get("entry") or call.get("status") == "excluded":
            continue
        if match_call(call, buys) is not None:      # took it on-level or loosely → not missed
            continue
        label, pct = classify(call)
        days = None
        if call.get("resolved_at"):
            try:
                days = (datetime.fromisoformat(call["resolved_at"]).date() - _fired_date(call)).days
            except ValueError:
                pass
        # corroborate against the ACTUAL post-alert move (real peak/trough from OHLC)
        tk = call["ticker"]
        if tk not in ohlc:
            ohlc[tk] = fl.load_ohlc(tk)
        exc = fl.excursion(ohlc[tk], call["fired_at"], call["entry"])
        out.append({
            "ticker": call["ticker"], "fired_on": str(_fired_date(call)),
            "fired_at": call.get("fired_at"),       # full timestamp (date + time)
            "entry": round(call["entry"], 2), "target": call.get("target"),
            "stop": call.get("stop"), "rr": call.get("rr"),
            "exit_price": call.get("exit_price"),   # counterfactual exit (target/stop/cap)
            "conviction": call.get("conviction"), "regime": call.get("regime_at_fire"),
            "outcome": label, "counterfactual_pct": pct, "realized_R": call.get("realized_R"),
            "exit_reason": call.get("exit_reason"), "days_to_exit": days,
            "corroboration": _corroboration(call, label),
            "actual_peak_pct": exc["peak_pct"] if exc else None,      # real MFE from the alert level
            "actual_trough_pct": exc["trough_pct"] if exc else None,  # real MAE
            "window_complete": exc["complete"] if exc else False,
        })
    out.sort(key=lambda m: (m["counterfactual_pct"] is None, -(m["counterfactual_pct"] or 0)))
    # Dedupe: the ledger can hold several rows for one opportunity (the same level
    # re-fired on a day). Collapse by ticker + date + ~level (round ₹) so a single
    # missed trade isn't counted 4× in the roll-up. Sorted best-first → keep the best.
    seen, deduped = set(), []
    for m in out:
        key = (m["ticker"], m["fired_on"], round(m["entry"]))
        if key not in seen:
            seen.add(key)
            deduped.append(m)
    return deduped


def summarize(missed: list[dict]) -> str:
    winners = [m for m in missed if m["outcome"] == "missed_winner"]
    dodged = [m for m in missed if m["outcome"] == "dodged_loser"]
    pending = [m for m in missed if m["outcome"] == "pending"]
    # confirmed = the verdict is corroborated by real price (target genuinely hit / stop hit),
    # not a time-cap "soft" outcome whose target never printed.
    confirmed = [m for m in missed if m.get("corroboration") in ("target_hit", "stopped")]
    soft = [m for m in missed if m.get("corroboration") == "soft"]
    cgain = sum(m["counterfactual_pct"] or 0 for m in confirmed if (m["counterfactual_pct"] or 0) > 0)
    csaved = sum(m["counterfactual_pct"] or 0 for m in confirmed if (m["counterfactual_pct"] or 0) < 0)

    lines = ["📓 **Sable missed-trade review** (swing, advised-but-not-taken)", ""]
    lines.append(f"Missed calls: {len(missed)}  ·  ✅ {len(winners)} winners "
                 f"·  🛡️ {len(dodged)} dodged losers  ·  ⏳ {len(pending)} pending")
    lines.append(f"Corroborated by real price: {len(confirmed)} (target/stop genuinely hit)  ·  "
                 f"⚠️ {len(soft)} soft (time-cap, target never printed)")
    lines.append(f"Following the **confirmed** missed calls → net **{cgain + csaved:+.1f}%** "
                 f"(left on table +{cgain:.1f}%, dodged {csaved:.1f}%)")
    real_winners = [m for m in winners if m.get("corroboration") == "target_hit"]
    if real_winners:
        lines.append("\nTop confirmed missed winners (target actually printed):")
        for m in sorted(real_winners, key=lambda x: -(x["counterfactual_pct"] or 0))[:5]:
            ap = f", actually peaked +{m['actual_peak_pct']:.0f}%" if m.get("actual_peak_pct") is not None else ""
            lines.append(f"  • {m['ticker']} — advised ₹{m['entry']:,} on {m['fired_on']} → "
                         f"**+{m['counterfactual_pct']:.1f}%**{ap} (not taken)")
    return "\n".join(lines)


def main():
    ledger = fl.load_ledger()
    buys = load_user_buys()
    missed = build_missed(ledger, buys)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in missed) + "\n",
                   encoding="utf-8")
    print(summarize(missed))
    print(f"\nWrote {OUT}  ({len(missed)} missed calls)")


if __name__ == "__main__":
    main()
