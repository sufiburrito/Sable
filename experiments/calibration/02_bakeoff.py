"""02_bakeoff.py — the lean first-pass weighting bake-off.

WHAT THIS DOES
--------------
Reads the regime-tagged sample table (01_reconstruct.py) and scores five
weighting methods on the metrics that answer "would this have made money over
a >=3-month hold":

  M0  equal weight ...................... the null model (today's behavior)
  M2  IC weights shrunk halfway to equal . DeMiguel: 1/N is hard to beat
  M3  regime-conditional IC weights ...... separate weights per up/side/down
  M4  redundancy-scaled (HRP in spirit) .. down-weight the collinear trend cluster
  M6  economic prior (six-framework) ..... Sable's philosophy, not data-fit

For each method it prints, overall AND split by regime:
  rank-IC      — Spearman corr of the weighted composite vs the forward return
  top-return%  — mean forward return of the top-third most-confident samples
                 (the long-only "buy Sable's best calls" P&L), minus cost
  spread%      — top-third minus bottom-third mean return (the long-short edge)
  hit%         — share of top-third samples that were actually profitable

!!! THIS FIRST PASS IS IN-SAMPLE !!!
Every data-fit method (M2/M3/M4) derives its weights from the SAME samples it is
scored on, so its numbers are optimistic by construction. This pass is a shape
check — which ideas look alive, and whether regime-conditioning matters. The
honest out-of-sample (walk-forward) comparison is the next script. M0 and M6 use
no fitting, so their numbers are already honest and make a fair yardstick.

Pure offline. Run from the repo root:  python3 experiments/calibration/02_bakeoff.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_SAMPLES = _HERE / "data" / "samples.csv"
FACTORS = ["Trend", "Momentum", "Volume", "DMA-S", "DMA-X", "DMA-C", "RS"]
REGIMES = ["uptrend", "sideways", "downtrend"]
COST = 0.1   # one-way cost stub, in % (a placeholder; realistic costs come later)


def _rank_ic(composites, returns):
    from alert_bot.calibrate import spearman_ic
    return spearman_ic(list(composites), list(returns), min_samples=20)


def _score_block(df, composites, label):
    """Print one method's overall + per-regime metrics for a given composite series."""
    rets = df["fwd_return"]
    ic = _rank_ic(composites, rets)
    m = cl.bucket_metrics(composites, rets, frac=1 / 3, cost=COST)
    ic_s = f"{ic:+.3f}" if ic is not None else "  n/a"
    print(f"  {label:26s}  IC={ic_s}  top={m['top_return']:+6.2f}%  "
          f"spread={m['spread']:+6.2f}%  hit={m['hit_rate']:4.1f}%  n={len(df)}")
    for regime in REGIMES:
        mask = (df["regime"] == regime).to_numpy()
        sub = df[mask]
        if len(sub) < 6:
            continue
        sub_comp = composites[mask]
        ric = _rank_ic(sub_comp, sub["fwd_return"])
        rm = cl.bucket_metrics(sub_comp, sub["fwd_return"], frac=1 / 3, cost=COST)
        ric_s = f"{ric:+.3f}" if ric is not None else "  n/a"
        print(f"      {regime:10s}            IC={ric_s}  top={rm['top_return']:+6.2f}%  "
              f"spread={rm['spread']:+6.2f}%  hit={rm['hit_rate']:4.1f}%  n={len(sub)}")


def main() -> None:
    import pandas as pd
    if not _SAMPLES.exists():
        raise SystemExit(f"missing {_SAMPLES} — run 01_reconstruct.py first")
    df = pd.read_csv(_SAMPLES)

    print(f"samples={len(df)}  tickers={df['ticker'].nunique()}  "
          f"span={df['date'].min()} -> {df['date'].max()}")
    print(f"cost stub={COST}% one-way   *** IN-SAMPLE — shape check, not a verdict ***\n")

    # Derive each method's weights from the full table (in-sample, by design).
    w_m0 = cl.equal_weights(FACTORS)
    w_m2 = cl.shrink_weights(cl.ic_weights(df, FACTORS), lam=0.5)
    w_m3 = cl.regime_conditional_weights(df, FACTORS)
    w_m4 = cl.redundancy_weights(df, FACTORS)
    w_m6 = cl.economic_prior_weights(FACTORS)

    print("[M0] equal weight")
    _score_block(df, cl.composite_scores(df, w_m0, FACTORS), "overall")
    print("\n[M2] IC weights, shrunk 0.5 to equal")
    _score_block(df, cl.composite_scores(df, w_m2, FACTORS), "overall")
    print("\n[M3] regime-conditional IC weights")
    _score_block(df, cl.composite_scores_regime(df, w_m3, FACTORS), "overall")
    print("\n[M4] redundancy-scaled (HRP-spirit)")
    _score_block(df, cl.composite_scores(df, w_m4, FACTORS), "overall")
    print("\n[M6] economic prior (six-framework)")
    _score_block(df, cl.composite_scores(df, w_m6, FACTORS), "overall")

    # Show the actual weights so the leaderboard is interpretable.
    print("\nweights (mean-normalized to 1.0):")
    print(f"  {'factor':10s} {'M2':>7s} {'M4':>7s} {'M6':>7s}   |  "
          + "  ".join(f"M3:{r[:4]}" for r in REGIMES))
    for f in FACTORS:
        m3cells = "  ".join(f"{w_m3.get(r, {}).get(f, 1.0):6.2f}" for r in REGIMES)
        print(f"  {f:10s} {w_m2[f]:7.2f} {w_m4[f]:7.2f} {w_m6[f]:7.2f}   |  {m3cells}")


if __name__ == "__main__":
    main()
