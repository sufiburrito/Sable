"""04_volume_probe.py — does adding VOLUME to the regime gate help?

CONTEXT
-------
03_regime_detectors.py found the winning gate is D4 = `bear ∨ stressed` (HMM
direction OR GARCH turbulence). Its only weakness was whipsaw (4 false flips in
the melt-up). Both detectors are PRICE-only — GARCH reads close-to-close returns,
the proxy reads close. Volume is unused at the regime level. This probe asks, on
the SAME metrics, whether folding NIFTY volume in improves the objectives
(top-return, spread, downtrend-tax cut) or cuts the whipsaw — or doesn't.

CASES (all gate the M0 equal-weight composite, damp danger→0, same as 03):
  B1  bear ∨ stressed ................. the current winner (price-only baseline)
  V1  bear ∨ (stressed ∧ vol_high) .... volume as a CONFIRMATION filter (anti-whipsaw)
  V2  bear ∨ stressed ∨ vol_high ...... volume as a THIRD union leg (more aggressive)
  V3  vol_high ........................ volume ALONE (diagnostic: any gating power?)
  V4  bear ∨ blend_stressed ........... volume blended INTO the turbulence percentile

`vol_high` = relative-volume (volume / 20d avg) in the top third of its own
trailing history — point-in-time, reusing vol_states_from_series. `blend` = the
50/50 average of the conditional-vol percentile and the relative-volume percentile.

NOTHING IS BAKED IN. This is a sandbox probe to inform a decision. Index volume
is a feed proxy (not shares of NIFTY), so treat any volume edge here as weaker
than the same idea would be on single-stock volume.

Pure offline. Run from the repo root:  python3 experiments/calibration/04_volume_probe.py
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
MELTUP_START, MELTUP_END = "2023-04-01", "2024-09-01"
HMM_TO_REGIME = {"bull": "uptrend", "bear": "downtrend",
                 "sideways": "sideways", "volatile": "sideways"}
HI, LO, MINHIST = 0.67, 0.33, 21


def _date_map(series):
    return {ts.date().isoformat(): val for ts, val in series.items()}


def _major_peaks(close, min_drop=0.10):
    peaks, run_high, run_high_date, armed = [], close.iloc[0], close.index[0], True
    for ts, px in close.items():
        if px >= run_high:
            run_high, run_high_date, armed = px, ts, True
        elif armed and px <= run_high * (1 - min_drop):
            peaks.append(run_high_date)
            armed, run_high, run_high_date = False, px, ts
    return peaks


def _trailing_pct(series, min_history):
    """Point-in-time trailing percentile: share of past bars strictly below each bar."""
    import numpy as np
    vals = series.to_numpy(dtype="float64")
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        if i + 1 < min_history:
            continue
        out[i] = (vals[: i + 1] < vals[i]).mean()
    import pandas as pd
    return pd.Series(out, index=series.index)


def _metrics(df, comp):
    return cl.bucket_metrics(comp, df["fwd_return"], frac=1 / 3, cost=COST)


def main() -> None:
    import numpy as np
    import pandas as pd

    nifty = cl.load_benchmark(_NIFTY_5Y)
    close, vol = nifty["Close"], nifty["Volume"]
    print(f"benchmark: {len(close)} bars  {close.index[0].date()} -> {close.index[-1].date()}")

    # --- Detectors on NIFTY (price side: reuse 03's machinery) ---
    print("building detectors (HMM + GARCH refits, ~30-60s)...")
    hmm = cl.hmm_regime_timeline(close, vol).reindex(close.index).ffill()
    garch_state = cl.garch_vol_states(close).reindex(close.index).ffill()
    garch_vol = cl.garch_conditional_vol(close).reindex(close.index).ffill()

    # --- Volume side: relative volume → point-in-time turbulence state ---
    v = vol.replace(0, np.nan).ffill()                 # 10 zero-days → carry forward
    rel_vol = v / v.rolling(20, min_periods=5).mean()  # today vs its 20d norm
    vol_state = cl.vol_states_from_series(
        rel_vol.dropna(), hi=HI, lo=LO, min_history=MINHIST
    ).reindex(close.index).ffill()
    vol_high = (vol_state == "stressed")

    bear = (hmm == "bear")
    stressed = (garch_state == "stressed")

    # V4: blend conditional-vol percentile with relative-volume percentile, classify.
    pct_g = _trailing_pct(garch_vol, MINHIST)
    pct_v = _trailing_pct(rel_vol, MINHIST)
    blend = (pct_g + pct_v) / 2.0
    blend_stressed = (blend >= HI)

    # --- Danger series per case ---
    danger = {
        "B1 bear∨stress":        bear | stressed,
        "V1 bear∨(str∧vol)":     bear | (stressed & vol_high),
        "V2 bear∨str∨vol":       bear | stressed | vol_high,
        "V3 vol_high alone":     vol_high,
        "V4 bear∨blend(g,v)":    bear | blend_stressed,
    }

    # --- Mechanism stats: how does volume relate to the price signals? ---
    n = len(close)
    print(f"\nmechanism (over {n} NIFTY bars):")
    print(f"  bear days       {int(bear.sum()):5d}  ({bear.mean()*100:4.1f}%)")
    print(f"  stressed days   {int(stressed.sum()):5d}  ({stressed.mean()*100:4.1f}%)")
    print(f"  vol_high days   {int(vol_high.sum()):5d}  ({vol_high.mean()*100:4.1f}%)")
    print(f"  stressed∧vol_high  {int((stressed&vol_high).sum()):5d}  "
          f"(of {int(stressed.sum())} stressed, {((stressed&vol_high).sum()/max(1,stressed.sum()))*100:4.1f}% are volume-backed)")
    print(f"  vol_high∧¬stressed {int((vol_high&~stressed).sum()):5d}  "
          f"(volume spikes the GARCH signal MISSED)")

    # --- Detection lag + whipsaw ---
    peaks = [p for p in _major_peaks(close, 0.10) if p.year >= 2024]  # post-warmup only
    print("\ndetection lag (trading days; post-warmup peaks): "
          + ", ".join(p.date().isoformat() for p in peaks))
    for name, d in danger.items():
        lags = [("never" if cl.detection_lag(d, p) is None else str(cl.detection_lag(d, p))) for p in peaks]
        wf = cl.whipsaw_rate(d, MELTUP_START, MELTUP_END)
        print(f"  {name:20s}  lag={ '/'.join(lags):12s}  whipsaw={wf}")

    # --- Gate profit on the sample table ---
    df = pd.read_csv(_SAMPLES)
    df["regime"] = df["date"].map(_date_map(hmm)).map(HMM_TO_REGIME).fillna("sideways")
    eq = cl.equal_weights(FACTORS)
    base_comp = cl.composite_scores(df, eq, FACTORS)
    dn = (df["regime"] == "downtrend")

    base = _metrics(df, base_comp)
    base_dn = _metrics(df[dn], base_comp[dn])
    print("\n=== Gate profit (M0 composite, damp danger→0) ===")
    print(f"  {'case':20s}  {'top%':>7s} {'spread%':>8s}  {'dn-spread%':>11s}  {'tax cut':>8s}")
    print(f"  {'(ungated M0)':20s}  {base['top_return']:+7.2f} {base['spread']:+8.2f}  "
          f"{base_dn['spread']:+11.2f}  {'—':>8s}")
    for name, d in danger.items():
        scell = df["date"].map(_date_map(d.map({True: 'DANGER', False: 'safe'}))).fillna("safe")
        gated = cl.gate_composite(base_comp, scell, {"DANGER"}, damp=0.0)
        g = _metrics(df, gated)
        gdn = _metrics(df[dn], gated[dn])
        tax_cut = gdn["spread"] - base_dn["spread"]
        print(f"  {name:20s}  {g['top_return']:+7.2f} {g['spread']:+8.2f}  "
              f"{gdn['spread']:+11.2f}  {tax_cut:+8.2f}")


if __name__ == "__main__":
    main()
