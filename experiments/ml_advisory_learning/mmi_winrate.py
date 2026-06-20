#!/usr/bin/env python3
"""
experiments/ml_advisory_learning/mmi_winrate.py — EXPERIMENT (read-only, no production change).

Does the Market Mood Index predict trade outcomes? Joins the 14-year MMI history (datasets.db,
as-of the fire date) to the 63 resolved forward-ledger calls and asks: win-rate / realized R by
MMI zone & bin, the IC of the continuous value, the contrarian hypothesis (fear → better BUYs),
and whether the continuous value beats the current coarse zone-vote.

n=63 — wide bootstrap CIs; read as indicative, not conclusive.
"""
import bisect
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RNG = np.random.default_rng(7)


def load_calls() -> pd.DataFrame:
    led = [json.loads(l) for l in (ROOT / "data/forward_ledger.jsonl").read_text().splitlines() if l.strip()]
    return pd.DataFrame([{"fired": str(r["fired_at"])[:10], "win": int(r["status"] == "win"),
                          "realized_R": r["realized_R"], "alert_type": r.get("alert_type")}
                         for r in led if r.get("realized_R") is not None])


def mmi_asof():
    con = sqlite3.connect(str(ROOT / "datasets/datasets.db"))
    rows = con.execute("SELECT date, value FROM mmi ORDER BY date").fetchall()
    con.close()
    dates = [d for d, _ in rows]; vals = [v for _, v in rows]

    def lookup(day: str):
        i = bisect.bisect_right(dates, day) - 1      # most recent MMI on/before the fire date
        return vals[i] if i >= 0 else None
    return lookup


def zone(v):
    return ("Extreme Fear" if v < 30 else "Fear" if v < 50 else "Greed" if v < 70 else "Extreme Greed")


def ic_ci(x, y, b=2000):
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = ~np.isnan(x) & ~np.isnan(y)
    x, y = x[m], y[m]
    if len(x) < 5:
        return (np.nan, np.nan, np.nan)
    pt = pd.Series(x).corr(pd.Series(y), method="spearman")
    boots = [pd.Series(x[i]).corr(pd.Series(y[i]), method="spearman")
             for i in (RNG.integers(0, len(x), len(x)) for _ in range(b))]
    return (pt, *np.nanpercentile(boots, [5, 95]))


def table(df, by):
    g = df.groupby(by, observed=True).agg(n=("win", "size"), win_rate=("win", "mean"),
                                          mean_R=("realized_R", "mean"))
    for k, r in g.iterrows():
        print(f"    {str(k):<16} n={int(r['n']):>2}  win {r['win_rate']*100:>3.0f}%  mean R {r['mean_R']:+.3f}")


def main():
    calls = load_calls()
    look = mmi_asof()
    calls["mmi"] = calls["fired"].map(look)
    calls["zone"] = calls["mmi"].map(lambda v: zone(v) if v is not None else None)
    calls["bin"] = pd.cut(calls["mmi"], [0, 30, 50, 70, 100],
                          labels=["<30 ExFear", "30-50 Fear", "50-70 Greed", "70+ ExGreed"])
    print(f"Calls joined to an MMI value: {calls['mmi'].notna().sum()}/{len(calls)}  "
          f"(overall win {calls['win'].mean()*100:.0f}%)\n")

    buys = calls[calls["alert_type"] == "BUY"]
    print(f"=== BUY calls ({len(buys)}) — outcome by MMI zone at fire ===")
    table(buys, "zone")
    print(f"\n=== BUY calls — by MMI bin ===")
    table(buys, "bin")

    print("\n=== Does MMI rank BUY outcomes?  (Spearman IC, bootstrap 90% CI) ===")
    for name, y in [("win", buys["win"]), ("realized R", buys["realized_R"])]:
        pt, lo, hi = ic_ci(buys["mmi"], y)
        flag = "" if (lo <= 0 <= hi) else "  <-- CI excludes 0"
        print(f"    MMI value vs {name:<11} IC={pt:+.3f}  90% CI [{lo:+.3f}, {hi:+.3f}]{flag}")

    # contrarian hypothesis: fear (<50) should give better BUYs than greed (>=50)
    fear = buys[buys["mmi"] < 50]; greed = buys[buys["mmi"] >= 50]
    print(f"\n=== Contrarian test (BUY) — buy the fear? ===")
    print(f"    MMI < 50 (fear):   n={len(fear):>2}  win {fear['win'].mean()*100:>3.0f}%  mean R {fear['realized_R'].mean():+.3f}")
    print(f"    MMI >= 50 (greed): n={len(greed):>2}  win {greed['win'].mean()*100:>3.0f}%  mean R {greed['realized_R'].mean():+.3f}")

    # does the continuous value beat the current coarse zone-vote? (BUY vote: +1 if <50 else -1)
    vote = np.where(buys["mmi"] < 50, 1.0, -1.0)
    icv = ic_ci(buys["mmi"], buys["realized_R"])[0]
    ico = ic_ci(vote, buys["realized_R"])[0]
    print(f"\n=== Richest-use check (BUY, vs realized R) ===")
    print(f"    continuous MMI value: IC={icv:+.3f}   |   current coarse vote (<50→+1): IC={ico:+.3f}")
    print(f"    {'continuous adds signal' if abs(icv) > abs(ico) + 0.02 else 'continuous ~= the coarse vote here'}")
    print("\nn=63 — indicative only. Re-run as the rig resolves more calls.")


if __name__ == "__main__":
    main()
