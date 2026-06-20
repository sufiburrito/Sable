#!/usr/bin/env python3
"""
forward_edge.py — the Bayesian "Both": per-class posterior edge + backtest discount.

From the resolved forward ledger, for each signal class (alert_type × conviction ×
regime) it computes:
  - a posterior WIN-RATE: Beta(κ·p' + wins, κ·(1−p') + losses), where p' is the
    backtest win-rate deflated by the regime discount δ (skeptical p=0.5 prior when
    no backtest anchor exists). Reports mean, 90% credible interval, P(win > 50%).
  - a posterior EXPECTANCY in R: Normal with a skeptical zero-edge prior (κ_e
    pseudo-obs at R=0). Reports mean, 90% CI, P(edge > 0).
  - δ per regime = forward/backtest win-rate, hierarchically shrunk toward 1 — the
    learned "how optimistic are our backtests" factor that deflates the priors.

Honest by construction: small classes get wide intervals; a class only "has an edge"
when P(edge>0) clears a bar AND its interval excludes zero. Research read, not a gate.

Usage:  python3 forward_edge.py
"""
import collections
import json
import math
from pathlib import Path

import numpy as np
from scipy.stats import beta as Beta, norm

import forward_lib as fl

OUT = Path(__file__).parent / "results" / "forward_edge"
KAPPA_WR = 8.0       # win-rate prior strength (pseudo-trades)
KAPPA_E = 5.0        # expectancy prior strength (pseudo-trades at R=0)
KAPPA_D = 10.0       # δ shrink-toward-1 strength
CI = 0.90


def _closed(rows):
    return [r for r in rows if r["status"] in ("win", "loss", "flat") and r.get("realized_R") is not None]


def _regime_discount(closed) -> dict:
    """δ_regime = forward win-rate / mean backtest win-rate, shrunk toward 1."""
    out = {}
    by_reg = collections.defaultdict(list)
    for r in closed:
        by_reg[r["regime_at_fire"]].append(r)
    for reg, rows in by_reg.items():
        wr_fwd = np.mean([r["status"] == "win" for r in rows])
        bt = [r["bt_winrate"] for r in rows if r.get("bt_winrate")]
        if not bt:
            out[reg] = 1.0
            continue
        raw = wr_fwd / max(1e-6, float(np.mean(bt)))
        n = len(rows)
        out[reg] = (KAPPA_D * 1.0 + n * raw) / (KAPPA_D + n)   # shrink toward 1
    return out


def _posterior(rows, delta_reg) -> dict:
    n = len(rows)
    wins = sum(r["status"] == "win" for r in rows)
    losses = n - wins
    R = np.array([r["realized_R"] for r in rows], dtype=float)

    # win-rate prior: deflated backtest, else skeptical 0.5
    bt = [r["bt_winrate"] for r in rows if r.get("bt_winrate")]
    reg = rows[0]["regime_at_fire"]
    if bt:
        p_prior = float(np.clip(delta_reg.get(reg, 1.0) * np.mean(bt), 0.02, 0.98))
    else:
        p_prior = 0.5
    a = KAPPA_WR * p_prior + wins
    b = KAPPA_WR * (1 - p_prior) + losses
    wr_mean = a / (a + b)
    wr_lo, wr_hi = Beta.ppf([(1 - CI) / 2, 1 - (1 - CI) / 2], a, b)
    p_win_gt_half = float(Beta.sf(0.5, a, b))

    # expectancy posterior: skeptical zero-edge Normal prior
    mu = (KAPPA_E * 0.0 + n * R.mean()) / (KAPPA_E + n)
    # Floor the sd: even identical outcomes don't make us certain of the true mean
    # (irreducible trade-outcome variance ~0.5R), and it avoids a 0-scale Normal.
    sd = max(R.std(ddof=1) if n > 1 else abs(R.mean()) + 1.0, 0.5)
    se = sd / math.sqrt(KAPPA_E + n)
    e_lo, e_hi = norm.ppf([(1 - CI) / 2, 1 - (1 - CI) / 2], mu, se)
    p_edge = float(norm.sf(0.0, mu, se))

    return {"n": n, "wins": wins, "p_prior": round(p_prior, 3),
            "wr_mean": round(float(wr_mean), 3), "wr_ci": [round(float(wr_lo), 3), round(float(wr_hi), 3)],
            "P_win_gt_50": round(p_win_gt_half, 3),
            "exp_R": round(float(mu), 3), "exp_ci": [round(float(e_lo), 3), round(float(e_hi), 3)],
            "P_edge_gt_0": round(p_edge, 3)}


def main():
    rows = fl.load_ledger()
    closed = _closed(rows)
    if not closed:
        print("No resolved calls yet — run backfill_ledger.py then forward_resolve.py.")
        return
    delta = _regime_discount(closed)

    classes = collections.defaultdict(list)
    for r in closed:
        classes[(r["alert_type"], r.get("conviction"), r["regime_at_fire"])].append(r)

    report = {m: _posterior(g, delta) for m, g in classes.items()}
    OUT.mkdir(parents=True, exist_ok=True)
    out = {"n_closed": len(closed), "regime_discount": {k: round(v, 3) for k, v in delta.items()},
           "classes": {f"{a}|{c}|{r}": v for (a, c, r), v in report.items()}}
    (OUT / "edge.json").write_text(json.dumps(out, indent=2))

    print(f"\nForward edge — {len(closed)} resolved calls "
          f"(of {sum(1 for r in rows if r['status']!='excluded')} fired, "
          f"{sum(1 for r in rows if r['status']=='open')} still open)\n")
    print("regime discount δ (forward/backtest, <1 ⇒ backtests optimistic): "
          + "  ".join(f"{k}={v:.2f}" for k, v in delta.items()))
    print(f"\n{'class (type·tier·regime)':<26}{'n':>3}{'winRate [90% CI]':>22}"
          f"{'P>50%':>7}{'E[R] [90% CI]':>22}{'P(R>0)':>8}")
    for (a, c, r), v in sorted(report.items(), key=lambda kv: -kv[1]["P_edge_gt_0"]):
        cls = f"{a}·{c}·{r}"
        wr = f"{v['wr_mean']:.2f} [{v['wr_ci'][0]:.2f},{v['wr_ci'][1]:.2f}]"
        er = f"{v['exp_R']:+.2f} [{v['exp_ci'][0]:+.2f},{v['exp_ci'][1]:+.2f}]"
        print(f"{cls:<26}{v['n']:>3}{wr:>22}{v['P_win_gt_50']:>7.2f}{er:>22}{v['P_edge_gt_0']:>8.2f}")

    # liquidity-stratified overall (a sanity cut, not a class)
    print("\nby liquidity (all closed):")
    for tier in ("liquid", "mid", "thin"):
        sub = [r for r in closed if r["liq_tier"] == tier]
        if sub:
            wr = np.mean([r["status"] == "win" for r in sub])
            mr = np.mean([r["realized_R"] for r in sub])
            print(f"  {tier:<7} n={len(sub):>3}  win-rate={wr*100:4.0f}%  mean R={mr:+.2f}")
    print(f"\nWrote {OUT/'edge.json'}")


if __name__ == "__main__":
    main()
