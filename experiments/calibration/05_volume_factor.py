"""05_volume_factor.py — does the per-stock VOLUME confidence factor earn its slot?

CONTEXT
-------
04_volume_probe.py showed volume is noise at the MARKET (NIFTY index) level.
This probe asks the cleaner question: at the PER-STOCK level, where volume is
real shares traded, does the existing Minervini volume-contraction factor
(`alert_bot.confidence._score_volume`: +1 when 5d vol contracts <0.8x the 50d
avg at a BUY, -1 when it expands >1.2x) actually predict the >=3-month forward
return — or is it dead weight in the composite?

Four tests, all from samples.csv (no refits, fast):
  1. STANDALONE IC — Spearman corr of each factor vs fwd_return, overall + by
     regime. Where does Volume rank? Does contraction predict?
  2. REDUNDANCY — Volume's correlation with the other six factors. Is it
     orthogonal (adds info) or collinear with the trend cluster (redundant)?
  3. IC-WEIGHT — what weight would the production ic_to_weights assign Volume?
  4. DROP-ONE (the decision) — equal-weight composite WITH all 7 factors vs
     WITHOUT Volume. If removing Volume hurts spread/top-return, it pulls its
     weight; if it helps or is neutral, it's dead weight.

Regime here is the sample table's tag (trailing-126d proxy) — fine for factor
stratification. NOTHING IS BAKED IN; this informs a decision.

Pure offline. Run from the repo root:  python3 experiments/calibration/05_volume_factor.py
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
COST = 0.1


def _ic(scores, rets):
    from alert_bot.calibrate import spearman_ic
    return spearman_ic(list(scores), list(rets), min_samples=20)


def main() -> None:
    import pandas as pd

    if not _SAMPLES.exists():
        raise SystemExit(f"missing {_SAMPLES} — run 01_reconstruct.py first")
    df = pd.read_csv(_SAMPLES)
    rets = df["fwd_return"]
    print(f"samples={len(df)}  tickers={df['ticker'].nunique()}  "
          f"span={df['date'].min()} -> {df['date'].max()}\n")

    # --- 1. Standalone IC, overall + by regime ---
    print("=== 1. Standalone IC (Spearman vs fwd_return) — Volume vs the field ===")
    print(f"  {'factor':10s} {'overall':>8s} " + " ".join(f"{r[:4]:>8s}" for r in REGIMES))
    for f in FACTORS:
        cells = []
        for r in REGIMES:
            sub = df[df["regime"] == r]
            ic = _ic(sub[f], sub["fwd_return"]) if len(sub) >= 20 else None
            cells.append(f"{ic:+.3f}" if ic is not None else "   n/a")
        oic = _ic(df[f], rets)
        flag = "  <-- VOLUME" if f == "Volume" else ""
        print(f"  {f:10s} {oic:+8.3f} " + " ".join(f"{c:>8s}" for c in cells) + flag)

    # --- 2. Redundancy: Volume's correlation with the other factors ---
    print("\n=== 2. Redundancy — Volume's |correlation| with each other factor ===")
    corr = df[FACTORS].corr()
    vol_corr = corr["Volume"].drop("Volume").abs().sort_values(ascending=False)
    for f, c in vol_corr.items():
        print(f"  Volume ~ {f:10s}  |r|={c:.3f}")
    print(f"  total |corr| (incl self) = {corr['Volume'].abs().sum():.2f}  "
          f"(higher = more redundant; mean across factors = "
          f"{corr.abs().sum().mean():.2f})")

    # --- 3. What weight would the production engine give Volume? ---
    print("\n=== 3. IC-weight the engine would assign (mean-normalized to 1.0) ===")
    w = cl.ic_weights(df, FACTORS, min_samples=30, ic_floor=0.0)
    for f in FACTORS:
        flag = "  <-- VOLUME" if f == "Volume" else ""
        print(f"  {f:10s} {w[f]:.3f}{flag}")

    # --- 4. DROP-ONE: does removing Volume help or hurt? (the decision) ---
    print("\n=== 4. Drop-one — equal-weight composite WITH vs WITHOUT Volume ===")
    eq_all = cl.equal_weights(FACTORS)
    no_vol = [f for f in FACTORS if f != "Volume"]
    eq_nov = cl.equal_weights(no_vol)

    def _report(label, factors, weights):
        comp = cl.composite_scores(df, weights, factors)
        m = cl.bucket_metrics(comp, rets, frac=1 / 3, cost=COST)
        oic = _ic(comp, rets)
        print(f"  {label:24s}  IC={oic:+.3f}  top={m['top_return']:+6.2f}%  "
              f"spread={m['spread']:+6.2f}%  hit={m['hit_rate']:4.1f}%")
        return m

    m_all = _report("7 factors (with Volume)", FACTORS, eq_all)
    m_nov = _report("6 factors (no Volume)", no_vol, eq_nov)
    print(f"  {'Δ from dropping Volume':24s}  "
          f"          top={m_nov['top_return'] - m_all['top_return']:+6.2f}%  "
          f"spread={m_nov['spread'] - m_all['spread']:+6.2f}%")
    print("  (positive Δ = dropping Volume IMPROVES the composite = Volume is dead weight)")

    # By-regime drop-one (where does Volume help, if anywhere?)
    print("\n  by regime (spread, with → without Volume):")
    for r in REGIMES:
        sub = df[df["regime"] == r]
        if len(sub) < 6:
            continue
        c_all = cl.composite_scores(sub, eq_all, FACTORS)
        c_nov = cl.composite_scores(sub, eq_nov, no_vol)
        s_all = cl.bucket_metrics(c_all, sub["fwd_return"], cost=COST)["spread"]
        s_nov = cl.bucket_metrics(c_nov, sub["fwd_return"], cost=COST)["spread"]
        print(f"    {r:10s}  {s_all:+6.2f}% -> {s_nov:+6.2f}%  (Δ {s_nov - s_all:+.2f})  n={len(sub)}")


if __name__ == "__main__":
    main()
