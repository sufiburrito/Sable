#!/usr/bin/env python3
"""
experiments/ml_advisory_learning/e3_online_edge.py — EXPERIMENT (read-only, no production change).

E3: the auto-learning backbone. A hierarchical Bayesian per-class edge that learns, from the
forward rig's resolved calls, the **expected realized R** of each context class
(alert_type | conviction | regime) — *targeting R, not win-rate*, because the diagnostic showed
high-conviction calls win often but earn nothing.

Statistics (small-data-appropriate, interpretable):
  - Partial pooling (James-Stein / empirical-Bayes): each class mean R is shrunk toward the global
    mean by τ²/(τ² + σ²/n_c), so a class with 1-2 samples barely moves off the global prior and a
    well-sampled class is trusted. σ² = pooled within-class R variance; τ² = between-class variance
    net of sampling noise. The principled way to handle 1-12 samples per cell.
  - Online by construction: a running (n, ΣR) per class is a conjugate incremental update as each new
    label resolves. The walk-forward below re-fits the growing prefix to mimic nightly accrual.

Honest evaluation: walk-forward with DELAYED labels — to score a call we train only on calls that had
already *resolved* before that call fired (no look-ahead). Graded against the diagnose.py baselines.
Nothing here writes to or imports into production.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def load() -> pd.DataFrame:
    led = [json.loads(l) for l in (ROOT / "data/forward_ledger.jsonl").read_text().splitlines() if l.strip()]
    rows = [{"realized_R": r["realized_R"], "win": int(r["status"] == "win"),
             "conviction": r.get("conviction"),
             "cls": f"{r.get('alert_type')}|{r.get('conviction')}|{r.get('regime_at_fire')}",
             "fired": str(r.get("fired_at"))[:10], "resolved": str(r.get("resolved_at"))[:10]}
            for r in led if r.get("realized_R") is not None]
    return pd.DataFrame(rows)


class HierEdge:
    """Empirical-Bayes hierarchical estimate of expected R (and win prob) per class."""

    def fit(self, df: pd.DataFrame) -> "HierEdge":
        self.mu = float(df["realized_R"].mean())
        self.global_win = float(df["win"].mean())
        g = df.groupby("cls")["realized_R"]
        self.n, self.xbar = g.count(), g.mean()
        self.sigma2 = max(float(g.var().mean()), 1e-6)            # pooled within-class R variance
        if len(self.xbar) > 1:                                    # between-class variance net of noise
            self.tau2 = max(float(self.xbar.var() - (self.sigma2 / self.n.clip(lower=1)).mean()), 0.0)
        else:
            self.tau2 = 0.0
        self.win_rate = df.groupby("cls")["win"].mean()
        return self

    def exp_R(self, cls: str) -> float:
        """Shrunk posterior expected R for a class (global mean if unseen)."""
        if cls not in self.n.index:
            return self.mu
        n = float(self.n[cls])
        denom = self.tau2 + self.sigma2 / n
        w = self.tau2 / denom if denom > 0 else 0.0             # trust the class in proportion to n
        return self.mu + w * (float(self.xbar[cls]) - self.mu)

    def p_win(self, cls: str, k0: float = 4.0) -> float:
        """Beta-Binomial win prob with a weak prior centred on the global rate."""
        a0, b0 = self.global_win * k0, (1 - self.global_win) * k0
        if cls not in self.n.index:
            return self.global_win
        n = float(self.n[cls]); wins = float(self.win_rate[cls]) * n
        return (a0 + wins) / (a0 + b0 + n)


def spearman(a, b) -> float:
    return float(pd.Series(np.asarray(a, float)).corr(pd.Series(np.asarray(b, float)), method="spearman"))


def walk_forward(df: pd.DataFrame, warmup: int = 18) -> dict:
    """For each call (by fire date), train ONLY on calls resolved before it fired, predict its R."""
    df = df.sort_values("fired").reset_index(drop=True)
    pR, pP, aR, aW, conv = [], [], [], [], []
    for _, row in df.iterrows():
        train = df[df["resolved"] <= row["fired"]]
        if len(train) < warmup or train["cls"].nunique() < 2:
            continue
        m = HierEdge().fit(train)
        pR.append(m.exp_R(row["cls"])); pP.append(m.p_win(row["cls"]))
        aR.append(row["realized_R"]); aW.append(row["win"]); conv.append(row["conviction"])
    aR, aW, pR, pP = map(lambda x: np.asarray(x, float), (aR, aW, pR, pP))
    half = np.median(pR)
    return {
        "n_test": len(aR),
        "IC_predR_vs_actualR": spearman(pR, aR),
        "IC_conviction_vs_actualR": spearman(conv, aR),
        "brier_model": float(np.mean((pP - aW) ** 2)),
        "brier_baseline": float(np.mean((aW.mean() - aW) ** 2)),
        "tophalf_meanR": float(aR[pR >= half].mean()), "bothalf_meanR": float(aR[pR < half].mean()),
    }


def main():
    df = load()
    print(f"Resolved calls: {len(df)}  ·  classes: {df['cls'].nunique()}  ·  mean R {df['realized_R'].mean():+.3f}\n")

    print("=== Walk-forward (delayed labels, no look-ahead) — does the learner beat baselines? ===")
    wf = walk_forward(df)
    print(f"  test calls scored out-of-sample: {wf['n_test']}")
    print(f"  IC(predicted R, actual R):   {wf['IC_predR_vs_actualR']:+.3f}   "
          f"(raw conviction {wf['IC_conviction_vs_actualR']:+.3f})")
    print(f"  Brier: model {wf['brier_model']:.4f}  vs baseline {wf['brier_baseline']:.4f}  "
          f"({'better' if wf['brier_model'] < wf['brier_baseline'] else 'NOT better'})")
    print(f"  mean actual R — top-half predicted {wf['tophalf_meanR']:+.3f}  "
          f"vs bottom-half {wf['bothalf_meanR']:+.3f}  "
          f"(spread {wf['tophalf_meanR']-wf['bothalf_meanR']:+.3f})")

    print("\n=== Learned edge (fit on all data) — posterior expected R per class ===")
    m = HierEdge().fit(df)
    table = sorted(((c, m.exp_R(c), m.p_win(c), int(m.n[c]), float(m.xbar[c])) for c in m.n.index),
                   key=lambda t: -t[1])
    print(f"  {'class (type|conv|regime)':<22}{'post.R':>8}{'rawR':>8}{'p_win':>7}{'n':>4}")
    for c, er, pw, n, raw in table:
        print(f"  {c:<22}{er:>+8.3f}{raw:>+8.3f}{pw:>7.2f}{n:>4}")

    print("\n=== Suggested advisory adjustment (where confidence and payoff disagree) ===")
    flagged = False
    for c, er, pw, n, raw in table:
        if pw >= 0.70 and er <= 0.05 and n >= 3:
            print(f"  DISCOUNT {c}: wins {pw*100:.0f}% but posterior R {er:+.3f} — high-prob, low-payoff.")
            flagged = True
        elif er >= 0.20 and n >= 3:
            print(f"  FAVOUR   {c}: posterior R {er:+.3f} on {n} calls — pays well.")
            flagged = True
    if not flagged:
        print("  (no class meets the n>=3 flag threshold yet — too little data per cell)")
    print("\nResearch read only. Grade every change against diagnose.py; re-run as labels accrue.")


if __name__ == "__main__":
    main()
