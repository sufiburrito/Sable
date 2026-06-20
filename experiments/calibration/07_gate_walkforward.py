"""07_gate_walkforward.py — is the D4 union gate's edge REAL out-of-sample, or a fluke?

CONTEXT
-------
03_regime_detectors.py crowned D4 = `bear ∨ stressed` (HMM bear direction OR GARCH
turbulence) the winning regime gate: full-sample top-return +41.66%, spread +13.79%
(+12pp over ungated), downtrend-tax cut +18.56. But that ranking was made with
full-sample hindsight — we *chose* D4 after seeing how it scored over all 5 years.
The detectors are point-in-time (each bar reads only past bars), but the CHOICE of
gate is not. So the honest worry remains: maybe the whole +12pp comes from one
episode (the 2022 drawdown) and the rule is useless going forward.

This probe splits the samples CHRONOLOGICALLY into thirds and asks whether the D4
gate helps — or at least does not hurt — in EACH fold, most importantly the most
recent one it was never tuned on. A gate that only pays off in fold 1 is a
single-episode artifact; a gate that cuts the tax in every fold is a real rule.

  TEST 1  walk-forward by chronological third — ungated vs D4-gated, overall and in
          downtrends. The tax cut must repeat across folds, not concentrate in one.
  TEST 2  damp sweep on the full sample — damp ∈ {1.0(=ungated), 0.5, 0.25, 0.0}.
          A softer gate that keeps most of the edge is safer against whipsaw than a
          hard damp→0 (the known D4 weakness: 4 false flips in the melt-up).

Walk-forward-honest by construction: the D4 timeline is built once on NIFTY with
point-in-time detectors; only the SAMPLES are partitioned by date. NOTHING BAKED IN.

Pure offline (NIFTY50_5y.csv + samples.csv). Run from the repo root:
    python3 experiments/calibration/07_gate_walkforward.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_NIFTY_5Y = _HERE / "data" / "NIFTY50_5y.csv"
_SAMPLES = _HERE / "data" / "samples.csv"

FACTORS = ["Trend", "Momentum", "Volume", "DMA-S", "DMA-X", "DMA-C", "RS"]
COST = 0.1
N_FOLDS = 3
HMM_TO_REGIME = {"bull": "uptrend", "bear": "downtrend",
                 "sideways": "sideways", "volatile": "sideways"}


def _date_map(series):
    return {ts.date().isoformat(): val for ts, val in series.items()}


def _spread(df, comp):
    return cl.bucket_metrics(comp, df["fwd_return"], frac=1 / 3, cost=COST)


def _fmt(m, key):
    v = m[key]
    return "    n/a" if v != v else f"{v:+6.2f}"


def main() -> None:
    import pandas as pd

    if not _NIFTY_5Y.exists() or not _SAMPLES.exists():
        raise SystemExit("missing NIFTY50_5y.csv or samples.csv — run 00/01 first")

    # --- Build the D4 union danger timeline ONCE on NIFTY (point-in-time) ---
    nifty = cl.load_benchmark(_NIFTY_5Y)
    close, vol = nifty["Close"], nifty["Volume"]
    print(f"benchmark: {len(close)} bars  {close.index[0].date()} -> {close.index[-1].date()}")
    print("building D4 detector (HMM + GARCH refits, ~30-60s)...")
    hmm = cl.hmm_regime_timeline(close, vol).reindex(close.index).ffill()
    garch = cl.garch_vol_states(close).reindex(close.index).ffill()
    union = (hmm == "bear") | (garch == "stressed")            # D4 danger flag
    danger_str = union.map({True: "DANGER", False: "safe"})

    # --- Join to samples; upgrade regime to the real HMM, attach the gate cell ---
    df = pd.read_csv(_SAMPLES).sort_values("date").reset_index(drop=True)
    df["regime"] = df["date"].map(_date_map(hmm)).map(HMM_TO_REGIME).fillna("sideways")
    df["cell"] = df["date"].map(_date_map(danger_str)).fillna("safe")

    eq = cl.equal_weights(FACTORS)
    comp = cl.composite_scores(df, eq, FACTORS)

    # === TEST 1: walk-forward by chronological third ===
    print(f"\nsamples={len(df)}  span {df['date'].min()} -> {df['date'].max()}")
    print("\n=== 1. Walk-forward: D4 gate (damp→0) by chronological third ===")
    print("  the tax cut must REPEAT across folds — not live in one episode\n")
    print(f"  {'fold (dates)':24s} {'n':>5s} {'dn':>4s}  "
          f"{'spread: un→gate':>17s}  {'dn-spread: un→gate':>20s}  {'taxcut':>7s}")

    bounds = [int(len(df) * k / N_FOLDS) for k in range(N_FOLDS + 1)]
    for k in range(N_FOLDS):
        lo, hi = bounds[k], bounds[k + 1]
        sub = df.iloc[lo:hi]
        c = comp.iloc[lo:hi]
        gated = cl.gate_composite(c, sub["cell"], {"DANGER"}, damp=0.0)
        dn = (sub["regime"] == "downtrend")
        un_all, g_all = _spread(sub, c), _spread(sub, gated)
        un_dn = _spread(sub[dn], c[dn])
        g_dn = _spread(sub[dn], gated[dn])
        tax = (g_dn["spread"] - un_dn["spread"]) \
            if (g_dn["spread"] == g_dn["spread"] and un_dn["spread"] == un_dn["spread"]) \
            else float("nan")
        label = f"{sub['date'].iloc[0]}→{sub['date'].iloc[-1]}"
        print(f"  {label:24s} {len(sub):5d} {int(dn.sum()):4d}  "
              f"{_fmt(un_all,'spread')}→{_fmt(g_all,'spread')}  "
              f"{_fmt(un_dn,'spread')}→{_fmt(g_dn,'spread')}  "
              f"{('   n/a' if tax!=tax else f'{tax:+6.2f}'):>7s}")

    # whole-sample reference row
    gated_all = cl.gate_composite(comp, df["cell"], {"DANGER"}, damp=0.0)
    dn_all = (df["regime"] == "downtrend")
    print(f"  {'─ full sample':24s} {len(df):5d} {int(dn_all.sum()):4d}  "
          f"{_fmt(_spread(df,comp),'spread')}→{_fmt(_spread(df,gated_all),'spread')}  "
          f"{_fmt(_spread(df[dn_all],comp[dn_all]),'spread')}→"
          f"{_fmt(_spread(df[dn_all],gated_all[dn_all]),'spread')}  "
          f"{'(ref)':>7s}")

    # === TEST 2: damp sweep on the full sample ===
    print("\n=== 2. Damp sweep (full sample) — how hard should the gate suppress? ===")
    print("  damp=1.0 is the ungated baseline (regression guard); lower = harder gate\n")
    print(f"  {'damp':>5s}  {'top%':>7s} {'spread%':>8s} {'hit%':>6s}  {'dn-spread%':>11s}")
    for damp in [1.0, 0.5, 0.25, 0.0]:
        g = cl.gate_composite(comp, df["cell"], {"DANGER"}, damp=damp)
        m = _spread(df, g)
        mdn = _spread(df[dn_all], g[dn_all])
        print(f"  {damp:5.2f}  {m['top_return']:+7.2f} {m['spread']:+8.2f} "
              f"{m['hit_rate']:5.1f}  {_fmt(mdn,'spread'):>11s}")


if __name__ == "__main__":
    main()
