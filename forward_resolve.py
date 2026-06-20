#!/usr/bin/env python3
"""
forward_resolve.py — resolve open ledger calls against the OHLC that printed after them.

Each open BUY/SELL call (entry treated as triggered at fire-time, since the alert only
fires when price reaches the level) is walked forward up to the 63-day cap:
  - target hit first  → win,  realized_R = +R:R
  - stop hit first    → loss, realized_R = −1
  - same bar hits both → pessimistic: assume the stop went first
  - neither by the cap → time-cap close, realized_R = fractional (mark-to-market in R)
  - not enough sessions elapsed yet → stays open for the next run
Idempotent and incremental: only `open` rows with levels are touched.

Usage:  python3 forward_resolve.py
"""
import collections

import pandas as pd

import forward_lib as fl


def _close(row: dict, ts, price: float, reason: str, r: float) -> dict:
    row["status"] = "win" if r > 0 else ("loss" if r < 0 else "flat")
    row["resolved_at"] = str(pd.Timestamp(ts).date())
    row["exit_price"] = round(float(price), 2)
    row["exit_reason"] = reason
    row["realized_R"] = round(float(r), 3)
    return row


def resolve_row(row: dict, df: pd.DataFrame) -> bool:
    """Mutate row to a resolved state if possible; return True if resolved."""
    fired = pd.Timestamp(row["fired_at"]).tz_localize(None).normalize()
    fut = df.loc[df.index > fired]
    if fut.empty:
        return False
    fut = fut.iloc[:fl.HORIZON_CAP]
    entry, target, stop = row["entry"], row["target"], row["stop"]
    is_buy = row["alert_type"] == "BUY"

    for ts, bar in fut.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        hit_stop = (lo <= stop) if is_buy else (hi >= stop)
        hit_tgt = (hi >= target) if is_buy else (lo <= target)
        if hit_stop:                      # stop first (also wins a same-bar tie)
            _close(row, ts, stop, "stop", -1.0)
            return True
        if hit_tgt:
            _close(row, ts, target, "target", row["rr"])
            return True

    if len(fut) < fl.HORIZON_CAP:         # not enough sessions yet — leave open
        return False
    cl, ts = float(fut.iloc[-1]["Close"]), fut.index[-1]
    frac = (cl - entry) / (entry - stop) if is_buy else (entry - cl) / (stop - entry)
    _close(row, ts, cl, "time_cap", frac)
    return True


def main():
    rows = fl.load_ledger()
    cache: dict = {}
    resolved = 0
    for row in rows:
        if row["status"] != "open" or not row.get("target"):
            continue
        t = row["ticker"]
        if t not in cache:
            cache[t] = fl.load_ohlc(t)
        if cache[t] is None:
            continue
        if resolve_row(row, cache[t]):
            resolved += 1
    fl.write_ledger(rows)

    by_status = collections.Counter(r["status"] for r in rows)
    closed = [r for r in rows if r["status"] in ("win", "loss", "flat")]
    print(f"resolved {resolved} this run. Ledger status: {dict(by_status)}")
    if closed:
        wins = [r for r in closed if r["status"] == "win"]
        avg_r = sum(r["realized_R"] for r in closed) / len(closed)
        print(f"closed={len(closed)}  win-rate={len(wins)/len(closed)*100:.1f}%  "
              f"mean R={avg_r:+.3f}")


if __name__ == "__main__":
    main()
