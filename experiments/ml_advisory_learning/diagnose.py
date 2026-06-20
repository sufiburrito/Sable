#!/usr/bin/env python3
"""
experiments/ml_advisory_learning/diagnose.py — EXPERIMENT (read-only, no production change).

The make-or-break question BEFORE building any learner: does Sable's confidence actually
rank-order real outcomes? If it doesn't, no learner layered on top helps — the factors need
fixing, not tuning. Measured on the 63 market-verified labels (forward_ledger realized_R).

Only NON-LEAKY, available signals are tested:
  - conviction (1-4)  — Sable's confidence tier, set at fire-time (full coverage; clean).
  - bt_winrate        — backtest win-rate (partial coverage; clean — also tests "do backtests
                        predict forward?", the δ question).
  - per-class structure (alert_type|conviction|regime), evaluated LEAVE-ONE-OUT so it is not
    graded on the same rows it was fit on.
The continuous composite score is NOT logged for these trades (0/63), so it can't be calibrated
yet — itself a finding: the scoring history is young.

Everything is reported with a bootstrap CI because n=63 is tiny — the point estimates are noisy
and these intervals are what shrink as the forward rig resolves more calls. No look-ahead: a
label exists only once the rig has resolved the call against future OHLC.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RNG = np.random.default_rng(7)


def load() -> pd.DataFrame:
    led = [json.loads(l) for l in (ROOT / "data/forward_ledger.jsonl").read_text().splitlines() if l.strip()]
    rows = [{"ticker": r["ticker"], "realized_R": r["realized_R"], "win": int(r["status"] == "win"),
             "conviction": r.get("conviction"), "regime": r.get("regime_at_fire"),
             "alert_type": r.get("alert_type"), "bt_winrate": r.get("bt_winrate")}
            for r in led if r.get("realized_R") is not None]
    return pd.DataFrame(rows)


def ic_ci(x: pd.Series, y: pd.Series, b: int = 2000) -> tuple:
    """Spearman IC with a bootstrap 90% CI (the honest width at this sample size)."""
    m = x.notna() & y.notna()
    x, y = x[m].to_numpy(), y[m].to_numpy()
    n = len(x)
    if n < 5:
        return (float("nan"), float("nan"), float("nan"), n)
    point = pd.Series(x).corr(pd.Series(y), method="spearman")
    boots = []
    for _ in range(b):
        idx = RNG.integers(0, n, n)
        boots.append(pd.Series(x[idx]).corr(pd.Series(y[idx]), method="spearman"))
    lo, hi = np.nanpercentile(boots, [5, 95])
    return (point, lo, hi, n)


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def loo_group_rate(df: pd.DataFrame, keys: list) -> np.ndarray:
    """Leave-one-out predicted win-prob = mean win of OTHER rows sharing the same key group
    (falls back to the leave-one-out global mean for singleton groups). No leakage."""
    g = df.groupby(keys)["win"]
    gsum, gcnt = g.transform("sum"), g.transform("count")
    gmean = df["win"].sum()
    pred = np.where(gcnt > 1, (gsum - df["win"]) / (gcnt - 1),
                    (gmean - df["win"]) / (len(df) - 1))
    return np.asarray(pred, dtype=float)


def main():
    df = load()
    print(f"Labels: {len(df)}  ·  win rate {df['win'].mean()*100:.0f}%  ·  mean R {df['realized_R'].mean():+.3f}\n")

    print("=== Does each clean signal rank realized R?  (Spearman IC, bootstrap 90% CI) ===")
    for name, col in [("conviction", df["conviction"]), ("bt_winrate", df["bt_winrate"])]:
        p, lo, hi, n = ic_ci(col, df["realized_R"])
        flag = "" if (lo <= 0 <= hi) else "  <-- CI excludes 0"
        print(f"  {name:<12} IC={p:+.3f}  90% CI [{lo:+.3f}, {hi:+.3f}]  (n={n}){flag}")

    print("\n=== Reliability: conviction tier -> actual outcome (does confidence mean anything?) ===")
    tab = df.groupby("conviction").agg(n=("win", "size"), win_rate=("win", "mean"),
                                       mean_R=("realized_R", "mean"))
    for tier, row in tab.iterrows():
        print(f"  conviction {tier}:  n={int(row['n']):>2}  win-rate {row['win_rate']*100:>3.0f}%  "
              f"mean R {row['mean_R']:+.3f}")
    mono = tab["win_rate"].is_monotonic_increasing
    print(f"  monotone (higher conviction -> higher win rate)? {mono}")

    print("\n=== Discrimination vs a no-skill baseline (Brier, lower=better) ===")
    y = df["win"].to_numpy(dtype=float)
    base = brier(np.full(len(df), y.mean()), y)
    conv_p = loo_group_rate(df, ["conviction"])
    cls_p = loo_group_rate(df, ["alert_type", "conviction", "regime"])
    print(f"  baseline (predict the {y.mean()*100:.0f}% base rate for all): {base:.4f}")
    print(f"  conviction-tier rate (leave-one-out):                       {brier(conv_p, y):.4f}")
    print(f"  per-class (type|conviction|regime) rate (leave-one-out):    {brier(cls_p, y):.4f}")
    print(f"  per-class IC vs win: {ic_ci(pd.Series(cls_p), df['win'])[0]:+.3f}")

    print("\n=== Read ===")
    print("A signal only has usable edge if its IC CI clears 0 and/or it beats the baseline Brier.")
    print("At n=63 the CIs are wide by design — they tighten as the forward rig resolves more calls,")
    print("and the feature-rich learner only becomes possible once the (Jun-15-onward) factor vectors")
    print("resolve. Re-run this monthly; it is the scorecard every future model must beat.")


if __name__ == "__main__":
    main()
