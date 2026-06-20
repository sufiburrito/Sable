"""03_regime_detectors.py — the regime-change detector bake-off.

WHAT THIS DOES
--------------
The bake-off (02_bakeoff.py) found the dominant failure is the *downtrend tax*:
every weighting method loses ~18-20% of its top-minus-bottom spread in downtrends,
and no factor re-weighting fixes it. The only lever that can is a regime GATE —
suppress confidence when the market is dangerous. But a gate is only as good as
the detector feeding it, and our detectors are slow. This script benchmarks four
detectors as *gates*, on the three metrics that matter:

  D0  trailing-126d-return proxy ...... the crude baseline (today's sandbox tag)
  D1  production HMM direction ........ the stable classifier (bull/bear/...)
  D2  GARCH(1,1) turbulence ........... the fast vol alarm (calm/normal/stressed)
  D3  HMM × GARCH 2D ................... direction × turbulence, danger = bear∧stressed

For each detector it reports:
  detection lag — trading days from a NIFTY drawdown peak to the first danger flag
                  (lower = faster; the peaks are found FROM the series, not memory)
  whipsaw       — spurious danger flips inside the 2023→Sep-2024 melt-up (lower = calmer)
  gate profit   — overall top-return + spread, and the downtrend-regime spread,
                  with the gate applied vs the ungated M0 baseline (the tax reduction)

Walk-forward-honest by construction: every detector reads only past bars; the only
fixed knobs are the percentile/lookback thresholds, held at principled defaults.

Pure offline (NIFTY50_5y.csv + samples.csv). Run from the repo root:

    python3 experiments/calibration/03_regime_detectors.py
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

# The danger cells for the 2D detector: a bear direction is the tax; pairing it
# with elevated turbulence is the highest-conviction "falling knife" signal.
DANGER_CELLS = {"bear|stressed", "bear|normal"}
# A clean uptrend window for the whipsaw count (fixed config, not tuned to returns).
MELTUP_START, MELTUP_END = "2023-04-01", "2024-09-01"
# HMM 4-state → the sample table's 3-state regime axis.
HMM_TO_REGIME = {"bull": "uptrend", "bear": "downtrend",
                 "sideways": "sideways", "volatile": "sideways"}


def _date_map(series):
    """Re-key a Date-indexed series by 'YYYY-MM-DD' string for joining to samples."""
    out = {}
    for ts, val in series.items():
        out[ts.date().isoformat()] = val
    return out


def _major_peaks(close, min_drop=0.10):
    """Find peak dates that each precede a >= min_drop correction (from the series).

    Walks the close series tracking the running high; when price falls min_drop
    below a running high, the running-high date is recorded as a peak and the
    search resets from the trough. This surfaces the real drawdown peaks (2022
    correction, Oct-2024 top) without hardcoding dates from memory.
    """
    peaks = []
    run_high = close.iloc[0]
    run_high_date = close.index[0]
    armed = True
    for ts, px in close.items():
        if px >= run_high:
            run_high = px
            run_high_date = ts
            armed = True
        elif armed and px <= run_high * (1 - min_drop):
            peaks.append(run_high_date)
            armed = False           # one peak per drawdown leg
            run_high = px
            run_high_date = ts
    return peaks


def _spread(df, composites):
    m = cl.bucket_metrics(composites, df["fwd_return"], frac=1 / 3, cost=COST)
    return m


def main() -> None:
    import pandas as pd

    if not _NIFTY_5Y.exists():
        raise SystemExit(f"missing {_NIFTY_5Y} — run 00_backfill_nifty.py first")
    if not _SAMPLES.exists():
        raise SystemExit(f"missing {_SAMPLES} — run 01_reconstruct.py first")

    nifty = cl.load_benchmark(_NIFTY_5Y)
    close, vol = nifty["Close"], nifty["Volume"]
    closes_arr = close.to_numpy(dtype="float64")
    print(f"benchmark: {len(close)} bars  {close.index[0].date()} -> {close.index[-1].date()}")

    # --- Build the four detector timelines ON NIFTY (market-level, once) ---
    print("building detectors (HMM + GARCH refits, ~30-60s)...")
    proxy = pd.Series(
        [cl.classify_regime(closes_arr, i) for i in range(len(closes_arr))],
        index=close.index,
    )
    hmm = cl.hmm_regime_timeline(close, vol).reindex(close.index).ffill()
    garch = cl.garch_vol_states(close).reindex(close.index).ffill()
    cell2d = cl.combined_regime(hmm.fillna("sideways"), garch.fillna("normal"))

    # D4 = the UNION: bear (HMM) OR stressed (GARCH). Where D3 (AND) collapsed to
    # HMM-alone, the union is the one combination that can capture both edges —
    # HMM's downtrend-tax cut AND GARCH's profit lift / sub-day speed.
    union = (hmm == "bear") | (garch == "stressed")

    danger = {
        "D0 trail-126d": (proxy == "downtrend"),
        "D1 HMM dir":    (hmm == "bear"),
        "D2 GARCH turb": (garch == "stressed"),
        "D3 HMM×GARCH":  cell2d.isin(DANGER_CELLS),
        "D4 bear∨stress": union,
    }
    cells = {
        "D0 trail-126d": danger["D0 trail-126d"].map({True: "DANGER", False: "safe"}),
        "D1 HMM dir":    danger["D1 HMM dir"].map({True: "DANGER", False: "safe"}),
        "D2 GARCH turb": danger["D2 GARCH turb"].map({True: "DANGER", False: "safe"}),
        "D3 HMM×GARCH":  cell2d,
        "D4 bear∨stress": union.map({True: "DANGER", False: "safe"}),
    }
    danger_cells = {
        "D0 trail-126d": {"DANGER"}, "D1 HMM dir": {"DANGER"},
        "D2 GARCH turb": {"DANGER"}, "D3 HMM×GARCH": DANGER_CELLS,
        "D4 bear∨stress": {"DANGER"},
    }

    peaks = _major_peaks(close, min_drop=0.10)
    print("major drawdown peaks (>=10% correction, from the series): "
          + ", ".join(p.date().isoformat() for p in peaks))

    # --- Detection lag + whipsaw (measured on the NIFTY timelines) ---
    print("\n=== Detection lag (trading days from peak to first danger flag) ===")
    hdr = "  detector        " + "  ".join(f"{p.date().isoformat():>12s}" for p in peaks)
    print(hdr)
    for name, dser in danger.items():
        lags = []
        for p in peaks:
            lag = cl.detection_lag(dser, peak_date=p)
            lags.append("never" if lag is None else str(lag))
        print(f"  {name:14s}  " + "  ".join(f"{x:>12s}" for x in lags))

    print(f"\n=== Whipsaw (false danger flips, {MELTUP_START} -> {MELTUP_END}) ===")
    for name, dser in danger.items():
        w = cl.whipsaw_rate(dser, start=MELTUP_START, end=MELTUP_END)
        print(f"  {name:14s}  {w} flips")

    # --- Gate profit on the sample table ---
    df = pd.read_csv(_SAMPLES)
    # Upgrade the regime column to the real HMM (retire the crude proxy as truth).
    hmm_map = _date_map(hmm)
    df["regime"] = df["date"].map(hmm_map).map(HMM_TO_REGIME).fillna("sideways")

    eq = cl.equal_weights(FACTORS)
    base_comp = cl.composite_scores(df, eq, FACTORS)
    base = _spread(df, base_comp)
    base_dn = _spread(df[df["regime"] == "downtrend"],
                      base_comp[df["regime"] == "downtrend"])

    print("\n=== Gate profit (M0 equal-weight composite, gate damps danger cells to 0) ===")
    print(f"  {'detector':14s}  {'top%':>7s} {'spread%':>8s}  | "
          f"{'dn-spread%':>11s}  {'tax cut':>8s}")
    bd = "n/a" if base_dn["spread"] != base_dn["spread"] else f"{base_dn['spread']:+.2f}"
    print(f"  {'(ungated M0)':14s}  {base['top_return']:+7.2f} {base['spread']:+8.2f}  | "
          f"{bd:>11s}  {'—':>8s}")

    for name in danger:
        smap = _date_map(cells[name])
        scell = df["date"].map(smap).fillna("safe")
        gated = cl.gate_composite(base_comp, scell, danger_cells[name], damp=0.0)
        g = _spread(df, gated)
        dn_mask = (df["regime"] == "downtrend")
        gdn = _spread(df[dn_mask], gated[dn_mask])
        gdn_s = gdn["spread"]
        tax_cut = (gdn_s - base_dn["spread"]) if (gdn_s == gdn_s and base_dn["spread"] == base_dn["spread"]) else float("nan")
        gdn_str = "n/a" if gdn_s != gdn_s else f"{gdn_s:+.2f}"
        tax_str = "n/a" if tax_cut != tax_cut else f"{tax_cut:+.2f}"
        print(f"  {name:14s}  {g['top_return']:+7.2f} {g['spread']:+8.2f}  | "
              f"{gdn_str:>11s}  {tax_str:>8s}")

    print("\nregime mix (real HMM):")
    for r, c in df["regime"].value_counts().items():
        print(f"  {r:10s} {c:6d}  ({c / len(df) * 100:4.1f}%)")


if __name__ == "__main__":
    main()
