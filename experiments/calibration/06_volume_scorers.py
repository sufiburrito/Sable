"""06_volume_scorers.py — can a BETTER volume scorer beat 'no volume at all'?

CONTEXT
-------
05_volume_factor.py showed the production Minervini *contraction* scorer (5d/50d
volume ratio snapshot) has negative IC and HURTS the composite — dropping it
doubles the spread (+2.1%→+4.4%). But that condemns one specific scorer, not
volume itself. This probe re-scores volume at every sample date with two
*accumulation* formulations and asks whether either has real edge:

  CONTRACTION  (old) — +1 when 5d vol < 0.8x 50d avg at a BUY (the current factor)
  OBV-TREND          — On-Balance Volume vs its own trailing mean. OBV cumulates
                       signed volume (+vol on up-days, -vol on down-days); OBV
                       rising above its mean = net accumulation. Point-in-time
                       (OBV[t] uses only bars <= t).
  PV-CONFIRM         — price-volume confirmation: is volume heavier on up-days
                       than down-days over the window? Up-vol > down-vol =
                       buyers in control (+1); the reverse = distribution (-1).

THE BAR: a volume scorer earns its slot only if swapping it into the composite
beats the 6-factor composite that has NO volume factor (05's +4.38% spread).

Point-in-time, pure offline (OHLC caches + samples.csv). NOTHING BAKED IN.
Run from the repo root:  python3 experiments/calibration/06_volume_scorers.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_SAMPLES = _HERE / "data" / "samples.csv"
_ANALYSIS = _ROOT / "analysis"
REGIMES = ["uptrend", "sideways", "downtrend"]
COST = 0.1
WINDOW = 20


def _obv_trend_score(close, vol, window=WINDOW):
    """OBV vs its trailing mean. +1 accumulation / -1 distribution / 0 flat."""
    import numpy as np
    if len(close) < window + 2:
        return 0
    dirn = np.sign(np.diff(close))
    obv = np.concatenate([[0.0], np.cumsum(dirn * vol[1:])])
    recent = obv[-window:]
    if obv[-1] > recent.mean():
        return 1
    if obv[-1] < recent.mean():
        return -1
    return 0


def _pv_confirm_score(close, vol, window=WINDOW):
    """Up-day vs down-day average volume over the window. +1 buyers / -1 sellers."""
    import numpy as np
    if len(close) < window + 2:
        return 0
    ch = np.diff(close)[-window:]
    v = vol[1:][-window:]                       # volume aligned to each change
    up = v[ch > 0]
    dn = v[ch < 0]
    up_v = up.mean() if len(up) else 0.0
    dn_v = dn.mean() if len(dn) else 0.0
    if up_v > dn_v * 1.1:
        return 1
    if dn_v > up_v * 1.1:
        return -1
    return 0


def main() -> None:
    import numpy as np
    import pandas as pd
    from alert_bot.ohlc_cache import read_ohlc_cache

    df = pd.read_csv(_SAMPLES)
    # Re-score volume at each (ticker, date) sample, point-in-time from the cache.
    obv_col, pv_col = {}, {}
    for ticker, grp in df.groupby("ticker"):
        cache = read_ohlc_cache(ticker, analysis_dir=_ANALYSIS)
        if cache is None:
            continue
        closes = cache["Close"].to_numpy(dtype="float64")
        vols = cache["Volume"].to_numpy(dtype="float64")
        pos = {d.date().isoformat(): i for i, d in enumerate(cache.index)}
        for _, row in grp.iterrows():
            t = pos.get(row["date"])
            if t is None:
                continue
            c, v = closes[: t + 1], vols[: t + 1]
            obv_col[(ticker, row["date"])] = _obv_trend_score(c, v)
            pv_col[(ticker, row["date"])] = _pv_confirm_score(c, v)

    key = list(zip(df["ticker"], df["date"]))
    df["OBV"] = [obv_col.get(k, 0) for k in key]
    df["PV"] = [pv_col.get(k, 0) for k in key]

    rets = df["fwd_return"]
    from alert_bot.calibrate import spearman_ic

    def _ic(s, r):
        return spearman_ic(list(s), list(r), min_samples=20)

    # --- 1. IC of each volume formulation, overall + by regime ---
    print(f"samples={len(df)}  tickers={df['ticker'].nunique()}\n")
    print("=== 1. IC by volume scorer (Spearman vs fwd_return) ===")
    print(f"  {'scorer':16s} {'overall':>8s} " + " ".join(f"{r[:4]:>8s}" for r in REGIMES))
    for col, name in [("Volume", "contraction(old)"), ("OBV", "OBV-trend"),
                      ("PV", "pv-confirm")]:
        cells = []
        for r in REGIMES:
            sub = df[df["regime"] == r]
            ic = _ic(sub[col], sub["fwd_return"]) if len(sub) >= 20 else None
            cells.append(f"{ic:+.3f}" if ic is not None else "   n/a")
        oic = _ic(df[col], rets)
        print(f"  {name:16s} {oic:+8.3f} " + " ".join(f"{c:>8s}" for c in cells))

    # how often each fires + how they correlate with each other
    print("\n  vote mix (share +1 / 0 / -1) and cross-correlation:")
    for col, name in [("Volume", "contraction"), ("OBV", "OBV-trend"), ("PV", "pv-confirm")]:
        vc = df[col].value_counts(normalize=True)
        print(f"    {name:12s}  +1={vc.get(1,0)*100:4.1f}%  0={vc.get(0,0)*100:4.1f}%  "
              f"-1={vc.get(-1,0)*100:4.1f}%")
    print(f"    corr(OBV, pv-confirm) = {df['OBV'].corr(df['PV']):.3f}   "
          f"corr(OBV, contraction) = {df['OBV'].corr(df['Volume']):.3f}")

    # --- 2. Drop-IN test: swap each volume scorer into the composite ---
    print("\n=== 2. Composite with each volume scorer (equal weight) — beat 'no volume'? ===")
    base6 = ["Trend", "Momentum", "DMA-S", "DMA-X", "DMA-C", "RS"]

    def _report(label, factors):
        comp = cl.composite_scores(df, cl.equal_weights(factors), factors)
        m = cl.bucket_metrics(comp, rets, frac=1 / 3, cost=COST)
        oic = _ic(comp, rets)
        print(f"  {label:26s}  IC={oic:+.3f}  top={m['top_return']:+6.2f}%  "
              f"spread={m['spread']:+6.2f}%  hit={m['hit_rate']:4.1f}%")
        return m

    _report("6-factor (NO volume) ★bar", base6)
    _report("+ contraction (old)", base6 + ["Volume"])
    _report("+ OBV-trend", base6 + ["OBV"])
    _report("+ pv-confirm", base6 + ["PV"])
    _report("+ OBV & pv-confirm", base6 + ["OBV", "PV"])


if __name__ == "__main__":
    main()
