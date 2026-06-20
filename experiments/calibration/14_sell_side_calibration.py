"""14_sell_side_calibration.py — when does an EXIT/TRIM signal earn its keep?

CONTEXT
-------
Every prior probe was BUY-side: top-ranked names -> high forward return. SELL is
a DIFFERENT signal. "Avoid" fires on WEAK stocks (low composite); "trim" fires on
STRONG, OVEREXTENDED ones (Stage-3 topping). And Sable's core is never sold — so
this is *swing-trim* calibration. The question is not "did forward return go
negative" but "did trimming-and-reloading beat just holding, over a >=3-month span
— and in which regime?"

The sample is bull-skewed (mean fwd_return +29.9%, max +389%): the cost of cutting
a winner early is huge and the benefit of catching a pullback is bounded. Prior tell:
DMA-X extension predicts reversal +0.185 IC in DOWNTRENDS but ~0.000 in UPTRENDS —
momentum persists when the trend carries. Hypothesis to FALSIFY: overextension
predicts a tradeable pullback. Expectation: it FAILS in uptrend, pays only in
sideways/topping.

TWO STAGES (user chose both):
  STAGE 1 — the regime map. Build point-in-time overextension gauges (ext_30wk,
    ext_50d, rsi14). Measure each vs (a) the 63-day forward return and (b) the
    forward max DIP (the pullback magnitude the blended return washes out),
    regime-stratified. Negative return-IC / positive dip-IC = "extension predicts
    a pullback".
  STAGE 2 — the money test. Trim when ext_30wk is in its trailing top band
    (point-in-time, adaptive); reload when extension mean-reverts (to the MA, and
    a half-retrace variant); compare the swing round-trip to buy-and-hold, by
    regime. revert-rate (did the reload ever trigger?) is the headline diagnostic.

Point-in-time throughout; offline (cached OHLC + NIFTY50_5y.csv only). Run from
repo root:  python3 experiments/calibration/14_sell_side_calibration.py
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from experiments.calibration import calib_lib as cl  # noqa: E402

_NIFTY = _HERE / "data" / "NIFTY50_5y.csv"
_ANALYSIS = _ROOT / "analysis"
REGIMES = ["uptrend", "sideways", "downtrend"]
MA_LONG = 150          # 30-week MA (Weinstein) — the trim anchor
MA_MED = 50            # medium stretch gauge
FWD_S1 = 63           # Stage-1 short horizon (one quarter) — where pullbacks live
HORIZON = 126         # Stage-2 swing window (~6 months, within the >=3M objective)
TRIM_HI = 0.80        # trim when ext_30wk in its trailing top 20%
MIN_HIST = 126        # trailing history before a trim threshold is judged
COST = 0.1

# non-equity caches in analysis/ — indices/FX/commodities are not swing-trimmable
MACRO_EXCLUDE = {"DXY", "GOLD", "GOLDBEES", "INDIAVIX", "INR", "NIFTY50", "TIP", "VIX"}


def _rsi(close, period: int = 14):
    import pandas as pd
    s = pd.Series(close, dtype="float64").reset_index(drop=True)
    delta = s.diff()
    gain = delta.clip(lower=0.0).rolling(period, min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    return 100.0 - 100.0 / (1.0 + rs)


def main() -> None:
    import numpy as np
    import pandas as pd
    from alert_bot.calibrate import WARMUP, STRIDE, spearman_ic
    from alert_bot.ohlc_cache import read_ohlc_cache

    if not _NIFTY.exists():
        print(f"missing {_NIFTY} — run 00_backfill_nifty.py first.")
        return

    nifty = pd.read_csv(_NIFTY, parse_dates=["Date"]).set_index("Date").sort_index()
    nclose = nifty["Close"].to_numpy(dtype="float64")
    nidx = nifty.index

    tickers = sorted({p.name.split("_ohlc_cache.csv")[0]
                      for p in _ANALYSIS.glob("*_ohlc_cache.csv")} - MACRO_EXCLUDE)

    # rows for Stage 1 (one per strided point-in-time date) and Stage-2 trim events
    s1 = []          # dict(regime, ext_30wk, ext_50d, rsi14, fwd_ret, fwd_dip)
    trims = []       # dict(regime, roundtrip_ma, roundtrip_half, hold, reverted)

    for ticker in tickers:
        df = read_ohlc_cache(ticker, analysis_dir=_ANALYSIS)
        if df is None or len(df) < WARMUP + HORIZON + 1:
            continue
        close = df["Close"].to_numpy(dtype="float64")
        n = len(close)
        ext_long = cl.extension_above_ma(close, MA_LONG).to_numpy()
        ext_med = cl.extension_above_ma(close, MA_MED).to_numpy()
        rsi = _rsi(close).to_numpy()
        trim_flag = cl.trailing_top_pctile_flag(ext_long, hi=TRIM_HI,
                                                min_history=MIN_HIST).to_numpy()

        for t in range(WARMUP, n - 1, STRIDE):
            date_t = df.index[t]
            npos = int(nidx.searchsorted(date_t, side="right")) - 1
            if npos < 0:
                continue
            regime = cl.classify_regime(nclose, npos, lookback=126)

            # --- Stage 1 sample (needs the short forward horizon) ---
            if t + FWD_S1 < n and ext_long[t] == ext_long[t]:
                base = close[t]
                fwd_ret = (close[t + FWD_S1] - base) / base * 100.0
                s1.append({
                    "regime": regime,
                    "ext_30wk": ext_long[t], "ext_50d": ext_med[t], "rsi14": rsi[t],
                    "fwd_ret": fwd_ret,
                    "fwd_dip": cl.forward_max_dip(close, t, FWD_S1),
                })

            # --- Stage 2 trim event (needs the full swing window + a real stretch) ---
            if trim_flag[t] and ext_long[t] > 0 and t + HORIZON < n:
                fc = close[t: t + HORIZON + 1]
                fe = ext_long[t: t + HORIZON + 1]
                rt_ma, hold = cl.trim_reload_roundtrip(fc, fe, reload_ext=0.0, cost_pct=COST)
                # half-retrace: reload once the stretch halves (a less patient reload)
                rt_half, _ = cl.trim_reload_roundtrip(fc, fe, reload_ext=ext_long[t] * 0.5,
                                                      cost_pct=COST)
                reverted = bool((fe[1:] <= 0.0).any())
                trims.append({"regime": regime, "roundtrip_ma": rt_ma,
                              "roundtrip_half": rt_half, "hold": hold,
                              "reverted": reverted})

    s1 = pd.DataFrame(s1)
    tr = pd.DataFrame(trims)
    print(f"Stage-1 samples={len(s1)}  Stage-2 trim events={len(tr)}  tickers={len(tickers)}")

    def _ic(a, b):
        d = pd.DataFrame({"a": a, "b": b}).replace([np.inf, -np.inf], np.nan).dropna()
        return spearman_ic(list(d["a"]), list(d["b"]), min_samples=20) if len(d) >= 20 else None

    # === STAGE 1: does overextension predict a pullback? (regime-stratified) ===
    print("\n=== STAGE 1 — overextension vs forward outcome (IC, by regime) ===")
    print("  trim works where: fwd-return IC is NEGATIVE and fwd-dip IC is POSITIVE")
    for target, want in [("fwd_ret", "neg=good"), ("fwd_dip", "pos=good")]:
        print(f"\n  target = {target}  ({want})")
        print(f"    {'factor':9s} {'overall':>8s} " + " ".join(f"{r[:4]:>8s}" for r in REGIMES))
        for fac in ["ext_30wk", "ext_50d", "rsi14"]:
            ov = _ic(s1[fac], s1[target])
            cells = []
            for r in REGIMES:
                g = s1[s1["regime"] == r]
                cells.append(_ic(g[fac], g[target]))
            ostr = f"{ov:+.3f}" if ov is not None else "   n/a"
            cstr = " ".join((f"{c:+.3f}" if c is not None else "   n/a") for c in cells)
            print(f"    {fac:9s} {ostr:>8s} {cstr}")

    # === STAGE 2: did trim-and-reload beat hold? (the money test) ===
    print("\n=== STAGE 2 — trim-and-reload vs buy-and-hold on the swing unit ===")
    print("  edge = mean(roundtrip - hold); revert% = share of trims that reloaded")
    print(f"  {'regime':9s} {'n':>4s} {'revert%':>7s} {'hold':>8s} "
          f"{'trim@MA':>8s} {'edge@MA':>8s} {'trim@½':>8s} {'edge@½':>8s} {'win%@MA':>7s}")
    for r in REGIMES + ["ALL"]:
        g = tr if r == "ALL" else tr[tr["regime"] == r]
        if len(g) == 0:
            continue
        edge_ma = (g["roundtrip_ma"] - g["hold"]).mean()
        edge_half = (g["roundtrip_half"] - g["hold"]).mean()
        win_ma = (g["roundtrip_ma"] > g["hold"]).mean() * 100.0
        print(f"  {r:9s} {len(g):4d} {g['reverted'].mean()*100:6.1f}% "
              f"{g['hold'].mean():+8.2f} {g['roundtrip_ma'].mean():+8.2f} {edge_ma:+8.2f} "
              f"{g['roundtrip_half'].mean():+8.2f} {edge_half:+8.2f} {win_ma:6.1f}%")

    print("\n  read: a POSITIVE edge means trimming added return over holding the swing.")
    print("  expect uptrend edge strongly NEGATIVE (low revert% -> trims stuck in cash")
    print("  while the trend ran away); any positive edge should live in sideways/down.")


if __name__ == "__main__":
    main()
