"""calib_lib — pure, testable building blocks for the calibration bake-off sandbox.

WHY A SEPARATE LIBRARY
----------------------
The numbered driver scripts (01_reconstruct.py, 02_bakeoff.py) start with a
digit, so Python can't import them as modules. We keep all the *logic* worth
testing here, in an importable module, and let the numbered files stay thin
runners. Nothing in this directory is ever imported by alert_bot/ — this is a
throwaway experiment sandbox (see 00_backfill_nifty.py's header).

This module deliberately REUSES the production engine where it already exists:
  - the factor scorers from alert_bot.confidence (causal: they read only the
    tail of the frame, so scoring df[:t] never peeks past bar t), and
  - blended_forward_return from alert_bot.calibrate (the 63/126/252-day outcome).
The only genuinely new pure logic here is the regime tag.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Regime tagging — a point-in-time label from the benchmark's own trailing return
# ---------------------------------------------------------------------------

def classify_regime(
    closes,
    i: int,
    lookback: int = 126,
    up_thresh: float = 5.0,
    down_thresh: float = -5.0,
) -> str:
    """Tag bar `i` as 'uptrend' | 'downtrend' | 'sideways' | 'unknown'.

    The label is the sign/size of the benchmark's trailing return over the last
    `lookback` bars: NIFTY up >= +5% over ~6 months ⇒ uptrend, down <= -5% ⇒
    downtrend, in between ⇒ sideways. It reads ONLY bars <= i (closes[i-lookback]
    and closes[i]), so it is point-in-time honest — no look-ahead. This is why we
    stratify the IC study by it: averaging the melt-up's positive trend-IC against
    the crash's negative trend-IC over one flat window cancels to ~zero and hides
    the real, regime-dependent signal.

    `unknown` when there isn't `lookback` bars of history behind `i`, or the base
    price is non-positive (bad data) — such samples are excluded from per-regime IC.
    """
    if i < lookback:
        return "unknown"
    base = float(closes[i - lookback])
    if base <= 0:
        return "unknown"
    ret = (float(closes[i]) - base) / base * 100.0
    if ret >= up_thresh:
        return "uptrend"
    if ret <= down_thresh:
        return "downtrend"
    return "sideways"


# ---------------------------------------------------------------------------
# Reconstruction — a granular, regime-tagged sample table
# ---------------------------------------------------------------------------
#
# Production `alert_bot.calibrate.reconstruct_samples` POOLS every sample into
# one flat (scores, returns) list per factor and throws the date away. For the
# bake-off we need the opposite: one ROW per (ticker, date) sample, keeping the
# date and regime, so we can later slice the IC by regime and by segment. We
# reuse the production scorers and the forward-return math unchanged — only the
# bookkeeping (keep rows, attach regime) is new.

def load_benchmark(csv_path: Path) -> pd.DataFrame:
    """Load the backfilled 5-year NIFTY file as a Date-indexed, tz-naive frame.

    This reads the SEPARATE experiment file (NIFTY50_5y.csv), never the live
    2-year cache the bot overwrites at market open — see 00_backfill_nifty.py.
    """
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def reconstruct_table(
    tickers: list[str],
    nifty_df: pd.DataFrame,
    analysis_dir: Path,
    regime_lookback: int = 126,
) -> pd.DataFrame:
    """One row per (ticker, date): the 7 price-factor scores + regime + forward return.

    Mirrors `calibrate.reconstruct_samples` (same WARMUP / STRIDE / forward
    windows, same causal df[:t+1] truncation) but emits a tidy DataFrame instead
    of pooled lists. Relative Strength is scored against the benchmark sliced to
    the sample date; the regime tag is the benchmark's trailing-return state at
    that same date.
    """
    # Imported lazily so importing this module never drags in the production
    # engine (and its deps) unless reconstruction is actually run.
    from alert_bot.calibrate import (
        PRICE_FACTORS, WARMUP, STRIDE, FORWARD_WINDOWS, blended_forward_return,
    )
    from alert_bot.confidence import _score_relative_strength
    from alert_bot.ohlc_cache import read_ohlc_cache

    nifty_closes = nifty_df["Close"].to_numpy(dtype="float64")
    nifty_index = nifty_df.index
    max_fwd = max(FORWARD_WINDOWS)
    rows: list[dict] = []

    for ticker in tickers:
        df = read_ohlc_cache(ticker, analysis_dir=analysis_dir)
        if df is None or len(df) < WARMUP + max_fwd + 1:
            continue
        closes = df["Close"].to_numpy(dtype="float64")
        n = len(df)
        for t in range(WARMUP, n - max_fwd, STRIDE):
            fr = blended_forward_return(closes, t)
            if fr is None:
                continue
            date_t = df.index[t]
            # Last benchmark bar on/before the sample date (exchanges share the
            # calendar, but be defensive about the odd missing bar).
            npos = int(nifty_index.searchsorted(date_t, side="right")) - 1
            if npos < 0:
                continue
            nslice = nifty_df.iloc[: npos + 1]
            if len(nslice) < 63:
                continue   # benchmark history too short for RS — skip the sample
            past = df.iloc[: t + 1]
            row: dict = {
                "ticker": ticker,
                "date": str(date_t.date()),
                "regime": classify_regime(nifty_closes, npos, lookback=regime_lookback),
                "fwd_return": fr,
            }
            for name, fn in PRICE_FACTORS.items():
                row[name] = fn(past, "BUY").score
            row["RS"] = _score_relative_strength(past, "BUY", nifty=nslice).score
            rows.append(row)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Weighting methods — each maps the sample table to a per-factor weight set
# ---------------------------------------------------------------------------
#
# Every method returns a {factor: weight} dict, mean-normalized so the average
# weight is ~1.0 (keeping the composite on the same scale as the equal-weight
# sum). M3 (regime-conditional) instead returns {regime: {factor: weight}}.

# Sable's six-framework hierarchy as a fixed economic prior (M6): trend
# structure leads, dual-momentum's relative leg next, volume is confirmation
# (not leading), RSI is weak at multi-month horizons, extension is a risk flag.
# Deliberately NOT fit to the data — it tests whether philosophy alone competes.
ECON_PRIOR = {
    "Trend": 1.3, "RS": 1.2, "DMA-S": 1.1, "DMA-C": 1.0,
    "Volume": 0.9, "Momentum": 0.8, "DMA-X": 0.7,
}


def _mean_normalize(weights: dict[str, float]) -> dict[str, float]:
    """Rescale so the mean weight is exactly 1.0 (no-op if the mean is 0)."""
    vals = list(weights.values())
    m = sum(vals) / len(vals) if vals else 0.0
    if m <= 0:
        return {k: 1.0 for k in weights}
    return {k: v / m for k, v in weights.items()}


def equal_weights(factors: list[str]) -> dict[str, float]:
    """M0 — the null model. Every vote equally loud (today's behavior)."""
    return {f: 1.0 for f in factors}


def ic_weights(
    df: pd.DataFrame, factors: list[str], min_samples: int = 30, ic_floor: float = 0.0
) -> dict[str, float]:
    """M1 — weight each factor by its Spearman IC, mean-normalized to 1.0.

    Reuses the production IC→weight conversion so the bake-off and the live
    engine agree on the mapping. ic_floor defaults to 0 here (vs the engine's
    conservative 0.02) so the bake-off can see each factor's full IC effect.
    """
    from alert_bot.calibrate import spearman_ic, ic_to_weights

    ic: dict[str, Optional[float]] = {}
    n: dict[str, int] = {}
    rets = df["fwd_return"].tolist()
    for f in factors:
        ic[f] = spearman_ic(df[f].tolist(), rets, min_samples=min_samples)
        n[f] = len(df)
    return ic_to_weights(ic, n, min_samples=min_samples, ic_floor=ic_floor)


def shrink_weights(weights: dict[str, float], lam: float) -> dict[str, float]:
    """M2 — pull weights toward the equal-weight 1.0 by (1-lam).

    lam=1 keeps the input weights; lam=0 collapses everything to 1.0. The
    DeMiguel-Garlappi-Uppal lesson is that 1/N is hard to beat, so shrinking
    a data-fit weight set toward equal usually *improves* out-of-sample.
    """
    return {k: 1.0 + lam * (v - 1.0) for k, v in weights.items()}


def redundancy_weights(df: pd.DataFrame, factors: list[str]) -> dict[str, float]:
    """M4 — down-weight factors that are collinear with the rest (HRP in spirit).

    A factor that co-moves with many others is voting for an idea the group
    already votes for (e.g. Trend/DMA-S/DMA-C/RS are one trend idea with four
    votes). We weight each factor by the inverse of its total absolute
    correlation, so an orthogonal factor (DMA-X, Momentum) earns more loudness
    than a member of a redundant cluster. Mean-normalized to 1.0.
    """
    corr = df[factors].corr().abs().fillna(0.0)
    redundancy = corr.sum(axis=1)               # includes self-corr (=1)
    inv = {f: (1.0 / redundancy[f] if redundancy[f] > 0 else 1.0) for f in factors}
    return _mean_normalize(inv)


def economic_prior_weights(factors: list[str]) -> dict[str, float]:
    """M6 — Sable's six-framework hierarchy as a fixed, non-fit prior."""
    return _mean_normalize({f: ECON_PRIOR.get(f, 1.0) for f in factors})


def regime_conditional_weights(
    df: pd.DataFrame, factors: list[str], min_samples: int = 30
) -> dict[str, dict[str, float]]:
    """M3 — a SEPARATE IC weight set per regime.

    This is the method the stratified-IC finding makes mandatory: the trend
    family's correct weight flips sign between uptrend and downtrend, so one
    global set is mis-specified. 'unknown' samples are skipped.
    """
    out: dict[str, dict[str, float]] = {}
    for regime, sub in df.groupby("regime"):
        if regime == "unknown":
            continue
        out[str(regime)] = ic_weights(sub, factors, min_samples=min_samples)
    return out


# ---------------------------------------------------------------------------
# Scoring + profit metrics
# ---------------------------------------------------------------------------

def composite_scores(
    df: pd.DataFrame, weights: dict[str, float], factors: list[str]
) -> pd.Series:
    """Weighted composite per sample: sum(weight_f * score_f). Flat weights."""
    s = pd.Series(0.0, index=df.index)
    for f in factors:
        s = s + float(weights.get(f, 1.0)) * df[f]
    return s


def composite_scores_regime(
    df: pd.DataFrame, regime_weights: dict[str, dict[str, float]], factors: list[str]
) -> pd.Series:
    """Composite using each sample's OWN regime weight set (for M3).

    Samples whose regime has no weight set (e.g. 'unknown') fall back to equal.
    """
    s = pd.Series(0.0, index=df.index)
    for regime, idx in df.groupby("regime").groups.items():
        w = regime_weights.get(str(regime), {})
        sub = df.loc[idx]
        ss = pd.Series(0.0, index=sub.index)
        for f in factors:
            ss = ss + float(w.get(f, 1.0)) * sub[f]
        s.loc[idx] = ss
    return s


# ---------------------------------------------------------------------------
# Regime detection — turbulence state, 2D combo, the gate, and detector metrics
# ---------------------------------------------------------------------------
#
# These are the building blocks of the detector bake-off (03_regime_detectors.py).
# All are point-in-time: a bar's label depends only on bars <= that bar, so the
# downtrend-tax measurement stays walk-forward-honest.

def vol_states_from_series(
    vol: pd.Series, hi: float = 0.67, lo: float = 0.33, min_history: int = 10
) -> pd.Series:
    """Classify each bar's volatility as 'calm' | 'normal' | 'stressed'.

    The state is the trailing *percentile* of that bar's vol within its OWN
    history (bars <= i): the fraction of past bars whose vol was strictly lower.
    >= `hi` of the history below it ⇒ unusually high vol ⇒ 'stressed'; <= `lo`
    ⇒ 'calm'; in between ⇒ 'normal'. We compare against the bar's own past, not
    a fixed threshold, so the detector adapts to the asset's vol level and never
    peeks ahead. Before `min_history` bars accumulate we can't judge stress, so
    the bar is 'normal' (neutral).
    """
    vals = vol.to_numpy(dtype="float64")
    states: list[str] = []
    for i in range(len(vals)):
        hist = vals[: i + 1]
        if len(hist) < min_history:
            states.append("normal")
            continue
        pct = float((hist < vals[i]).mean())   # share of past bars strictly below
        if pct >= hi:
            states.append("stressed")
        elif pct <= lo:
            states.append("calm")
        else:
            states.append("normal")
    return pd.Series(states, index=vol.index)


def combined_regime(direction: pd.Series, turbulence: pd.Series) -> pd.Series:
    """Pair a direction label with a turbulence label into a 2D cell, e.g.
    'bear|stressed' — the key the gate keys on."""
    return pd.Series(
        [f"{d}|{t}" for d, t in zip(direction, turbulence)],
        index=direction.index,
    )


def gate_composite(
    composite: pd.Series,
    cells: pd.Series,
    danger_cells: set,
    damp: float = 0.0,
) -> pd.Series:
    """Multiply the composite by `damp` inside `danger_cells`, leave it elsewhere.

    This is M3-GATE: suppress confidence (≈0) when the regime is dangerous
    (bear ∧ stressed), so we stop firing high-confidence BUYs into falling
    knives. `damp=1.0` is a no-op (the regression guard the ungated baseline uses).
    """
    factor = cells.apply(lambda c: damp if c in danger_cells else 1.0)
    return composite * factor


def detection_lag(danger: pd.Series, peak_date) -> Optional[int]:
    """Trading days from a known peak to the first danger flag at/after it.

    Lower is faster. Counts *bars* (positions in the series index), not calendar
    days, so it measures the detector's reaction speed in market sessions.
    Returns None if the detector never raised the flag at/after the peak.
    """
    peak = pd.Timestamp(peak_date)
    after = danger[danger.index >= peak]
    flagged = after[after.astype(bool)]
    if flagged.empty:
        return None
    pos_peak = int(danger.index.searchsorted(peak))
    pos_first = int(danger.index.searchsorted(flagged.index[0]))
    return pos_first - pos_peak


def whipsaw_rate(danger: pd.Series, start, end) -> int:
    """Count False→True danger flips inside [start, end].

    A clean calm-uptrend window should produce 0; every spurious flip into
    danger (and back) costs a count. This is the false-alarm half of the
    speed/stability tradeoff — a fast detector that whipsaws is not free.
    """
    win = danger[
        (danger.index >= pd.Timestamp(start)) & (danger.index <= pd.Timestamp(end))
    ].astype(bool).to_numpy()
    rises = 0
    prev = False
    for v in win:
        if v and not prev:
            rises += 1
        prev = bool(v)
    return rises


# ---------------------------------------------------------------------------
# Detector timelines on the benchmark — the GARCH (fast) and HMM (stable) sides
# ---------------------------------------------------------------------------
#
# Regime is a MARKET-level property, so we compute each detector ONCE on NIFTY
# across all of history and later join it to every sample by date. That makes
# the per-date refits affordable (~60 strided fits, not 2,519×). Both are
# point-in-time: at strided date t each model sees only bars < t (GARCH) or <= t
# (HMM), then the label is forward-filled until the next refit.

def garch_conditional_vol(
    nifty_close: pd.Series, refit_stride: int = 21, window: int = 504
) -> pd.Series:
    """1-step-ahead GARCH(1,1) conditional volatility on NIFTY, point-in-time.

    At each strided date t we fit GARCH(1,1) on the trailing `window` of (pct)
    log-returns ending at t-1, then take the 1-step conditional-vol forecast as
    the volatility AS-OF t. Volatility is the most autocorrelated market feature
    and (leverage effect) expands as prices break — so this is the *fast danger
    alarm*. Forward-filled between refits. Reads only past returns → no look-ahead.
    """
    from arch import arch_model

    closes = nifty_close.astype("float64")
    rets = 100.0 * np.log(closes / closes.shift(1))   # daily % log returns
    vol = pd.Series(np.nan, index=nifty_close.index, dtype="float64")
    n = len(closes)
    for t in range(window, n, refit_stride):
        sample = rets.iloc[t - window:t].dropna()
        if len(sample) < 100:
            continue
        try:
            res = arch_model(
                sample, vol="Garch", p=1, q=1, mean="Zero", dist="normal"
            ).fit(disp="off")
            fc = res.forecast(horizon=1, reindex=False)
            sigma = float(np.sqrt(fc.variance.values[-1, 0]))
        except Exception:
            continue
        vol.iloc[t] = sigma
    return vol.ffill()


def garch_vol_states(
    nifty_close: pd.Series,
    refit_stride: int = 21,
    window: int = 504,
    hi: float = 0.67,
    lo: float = 0.33,
) -> pd.Series:
    """GARCH turbulence timeline: 'calm' | 'normal' | 'stressed' (detector D2)."""
    vol = garch_conditional_vol(nifty_close, refit_stride, window).dropna()
    return vol_states_from_series(vol, hi=hi, lo=lo, min_history=refit_stride)


def hmm_regime_timeline(
    nifty_close: pd.Series,
    nifty_vol: pd.Series,
    refit_stride: int = 21,
    lookback: int = 504,
) -> pd.Series:
    """Production-HMM direction timeline: 'bull'|'bear'|'sideways'|'volatile' (D1).

    Reuses `quant_modeling.hmm_regime.run_regime_detection` UNCHANGED — at each
    strided t it runs on closes[:t]/volumes[:t] (Viterbi on that window only), so
    `current` is the regime as-of bar t-1. The HMM is the *stable classifier*
    (504-day window, deliberately sticky) — it answers WHICH discrete state, where
    GARCH answers HOW turbulent. Forward-filled between refits.
    """
    from quant_modeling.hmm_regime import run_regime_detection

    closes = nifty_close.astype("float64").tolist()
    vols = nifty_vol.astype("float64").tolist()
    out = pd.Series(index=nifty_close.index, dtype="object")
    n = len(closes)
    for t in range(lookback, n + 1, refit_stride):
        try:
            res = run_regime_detection(closes[:t], vols[:t], lookback_days=lookback)
            out.iloc[t - 1] = res["current"]
        except Exception:
            continue
    return out.ffill()


def bucket_metrics(
    composites, returns, frac: float = 1 / 3, cost: float = 0.1
) -> dict[str, float]:
    """Sort by composite; compare the top fraction's outcome to the bottom's.

    Returns the metrics that directly answer 'would this have made money':
      top_return — mean forward return of the top bucket, minus one-way cost
                   (the long-only 'buy Sable's most-confident names' P&L);
      spread     — top-minus-bottom mean return, minus round-trip cost
                   (the factor's long-short edge);
      hit_rate   — % of top-bucket samples with a positive forward return.
    NaN-filled when there are too few samples to form buckets.
    """
    d = pd.DataFrame({"c": list(composites), "r": list(returns)}).dropna()
    n = len(d)
    if n < 6:
        return {"top_return": float("nan"), "spread": float("nan"),
                "hit_rate": float("nan"), "n_bucket": 0}
    d = d.sort_values("c")
    k = max(1, int(n * frac))
    bottom = d.head(k)["r"]
    top = d.tail(k)["r"]
    return {
        "top_return": float(top.mean() - cost),
        "spread": float((top.mean() - bottom.mean()) - 2 * cost),
        "hit_rate": float((top > 0).mean() * 100.0),
        "n_bucket": int(k),
    }



# ---------------------------------------------------------------------------
# SELL-side (swing-trim) calibration primitives (Round 5)
# ---------------------------------------------------------------------------
#
# BUY-side asks "top-ranked names -> high forward return". SELL/trim is a
# DIFFERENT signal: it fires on STRONG, OVEREXTENDED names (Stage-3 topping),
# and — because Sable's core is never sold — it governs only the SWING layer.
# A trim is "right" when trimming-and-reloading beats holding over the span, NOT
# when forward return goes negative. These are the pure, point-in-time building
# blocks for that test; the runner (14_sell_side_calibration.py) wires them up.


def extension_above_ma(close, window: int):
    """Point-in-time stretch of price above its own trailing simple MA.

    `(close - SMA_window) / SMA_window`, a unitless overextension gauge: +0.20
    means price sits 20% above its `window`-bar average — the Weinstein Stage-3
    'too far above the 30-week line' tell. Causal by construction: pandas'
    rolling mean at bar t uses only bars <= t, so this never peeks ahead.
    """
    import pandas as pd
    s = pd.Series(close, dtype="float64").reset_index(drop=True)
    ma = s.rolling(window, min_periods=window).mean()
    return (s - ma) / ma


def forward_max_dip(close, i: int, horizon: int) -> float:
    """Deepest forward drop below the bar-`i` price within the next `horizon` bars.

    Returned as a POSITIVE magnitude in percent (0.0 if the path only ever rises).
    This is the quantity a trim-and-reload monetizes — the size of the pullback a
    trim could buy back into — and it is exactly what the blended 63/126/252-day
    forward return WASHES OUT (a sharp dip that fully recovers leaves the blend
    flat). Measuring it directly is why Stage 1 needs more than the BUY metric.
    """
    import numpy as np
    base = float(close[i])
    if base <= 0:
        return float("nan")
    end = min(i + horizon, len(close) - 1)
    if end <= i:
        return float("nan")
    fwd = np.asarray(close[i + 1: end + 1], dtype="float64")
    worst = float(fwd.min())
    dip = (worst - base) / base * 100.0
    return float(max(0.0, -dip))


def trim_reload_roundtrip(fwd_close, fwd_ext, reload_ext: float = 0.0,
                          cost_pct: float = 0.1):
    """Simulate trim-at-signal / reload-at-mean vs. buy-and-hold on the SWING unit.

    Inputs are the forward path FROM a trim event: `fwd_close[0]` is the price the
    trim fired at, `fwd_ext` is the matching extension path (e.g. extension_above_ma).

    Trim logic (one interpretable knob): sell the swing unit at `fwd_close[0]`; sit
    in cash until extension first mean-reverts to `<= reload_ext` (price back near
    its MA), then reload and ride to the window end. If it NEVER reverts within the
    window, the swing stays in cash (0% — the honest giveup of a false trim in a
    trend that ran away). Hold = just ride the swing from start to end.

    Returns (roundtrip_return_pct, hold_return_pct). `cost_pct` is charged per
    executed transaction (the sale always; the reload only if it happens), so
    trimming always pays at least the sale tax — there is no free trim.
    """
    import numpy as np
    fc = np.asarray(fwd_close, dtype="float64")
    fe = np.asarray(fwd_ext, dtype="float64")
    entry = float(fc[0])
    end = float(fc[-1])
    hold = (end / entry - 1.0) * 100.0

    # first bar (after the trim) where extension reverts to/below the reload band
    revert_k = None
    for k in range(1, len(fc)):
        if fe[k] == fe[k] and fe[k] <= reload_ext:   # not-NaN and reverted
            revert_k = k
            break

    if revert_k is None:
        roundtrip = 0.0 - cost_pct                    # stayed in cash; 1 txn (the sale)
    else:
        reload_price = float(fc[revert_k])
        roundtrip = (end / reload_price - 1.0) * 100.0 - 2.0 * cost_pct  # sale + reload
    return float(roundtrip), float(hold)


def trailing_top_pctile_flag(series, hi: float = 0.80, min_history: int = 126):
    """Point-in-time bool: is each value in the TOP `(1-hi)` of its own past?

    At bar t, the share of prior values strictly below today's is computed from
    bars <= t only; True when that share `>= hi` (today sits in the upper tail of
    everything seen so far). False until `min_history` observations accrue. This
    is how a trim THRESHOLD stays adaptive (no magic extension constant) and
    point-in-time — the SELL-side cousin of 12's bearish-extreme `danger` flag.
    """
    import numpy as np
    import pandas as pd
    v = pd.Series(series, dtype="float64").to_numpy()
    out = np.zeros(len(v), dtype=bool)
    for i in range(len(v)):
        if i + 1 < min_history or v[i] != v[i]:
            continue
        hist = v[: i + 1]
        hist = hist[~np.isnan(hist)]
        if len(hist) < min_history:
            continue
        share_below = float((hist[:-1] < v[i]).mean()) if len(hist) > 1 else 0.0
        out[i] = share_below >= hi
    return pd.Series(out, index=pd.Series(series).index if hasattr(series, "index") else None)
