"""
Floor/ceiling context for live alerts.

Reads pre-computed backtest stats (analysis/TICKER_backtest.json) and the OHLC
cache CSV to produce a one-line hint appended to each fired alert:

  BUY:   "Watch for better entry near ₹131"
  SELL:  "Rally may extend to ₹148 before trimming"

Blends three signals:
  1. ATR × 1.5 overshoot buffer (static volatility estimate)
  2. Backtest median drawdown (historical worst-case)
  3. Exponential Smoothing trend forecast (current momentum direction)

No network calls — only local file reads, safe to call during the poll loop.

Confidence rules:
  - No backtest JSON for ticker           → None (silent omission)
  - Level not in JSON or n == 0           → None (silent omission)
  - n < 3 (insufficient)                  → hint + "low confidence, rebuild chart"
  - n >= 3                                → hint, normal
"""
import json
import logging
from pathlib import Path

import pandas as pd

from .forecaster import trend_forecast
from .parser import AlertLevel

logger = logging.getLogger(__name__)

_ANALYSIS_DIR = Path("analysis")
_ATR_MULTIPLIER = 1.5      # ATR × this = overshoot buffer
_ZONE_CLUSTER_PCT = 0.025  # 2.5% — same as retrospective_analysis
_ZONE_AGREE_PCT = 0.02     # ATR floor and support zone must be within 2% to "agree"
_SWING_WINDOW = 5          # bars each side for local min/max detection


# ---------------------------------------------------------------------------
# OHLC helpers
# ---------------------------------------------------------------------------

def _load_ohlc(ticker: str) -> "pd.DataFrame | None":
    """Read the OHLC cache CSV without making any network calls."""
    csv_path = _ANALYSIS_DIR / f"{ticker}_ohlc_cache.csv"
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception as e:
        logger.debug(f"floor_context: OHLC read error for {ticker}: {e}")
        return None


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """14-period EWM ATR — same formula as retrospective_analysis.py."""
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


def _resistance_zones(df: pd.DataFrame) -> list[float]:
    """
    Swing-high clusters (mirror of support zones using High column).
    Returns list of cluster mean prices, sorted ascending.
    """
    highs = []
    n = len(df)
    for i in range(_SWING_WINDOW, n - _SWING_WINDOW):
        h = float(df["High"].iloc[i])
        window = df["High"].iloc[i - _SWING_WINDOW: i + _SWING_WINDOW + 1]
        if float(window.max()) <= h:
            highs.append(h)
    return _cluster(highs)


def _cluster(prices: list[float]) -> list[float]:
    """Group prices within ZONE_CLUSTER_PCT of each other, return cluster means."""
    import numpy as np
    if not prices:
        return []
    sorted_p = sorted(prices)
    groups: list[list[float]] = [[sorted_p[0]]]
    for p in sorted_p[1:]:
        ref = float(np.mean(groups[-1]))
        if abs(p - ref) / ref <= _ZONE_CLUSTER_PCT:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [float(np.mean(g)) for g in groups]


# ---------------------------------------------------------------------------
# Backtest sidecar
# ---------------------------------------------------------------------------

def _load_backtest(ticker: str) -> "dict | None":
    """Load analysis/TICKER_backtest.json. Returns None if missing/corrupt."""
    json_path = _ANALYSIS_DIR / f"{ticker}_backtest.json"
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"floor_context: backtest JSON read error for {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def floor_hint(level: AlertLevel, ticker: str, curr_price: float) -> "str | None":
    """
    Return the one-line floor/ceiling hint for a fired alert, or None.

    Called from main.py immediately after a crossing is detected.
    All file reads — no yfinance calls.
    """
    # Determine direction from alert type / current price
    if level.alert_type == "BUY":
        direction = "down"
    elif level.alert_type == "SELL":
        direction = "up"
    else:  # WATCH — infer from where price is now
        direction = "down" if curr_price <= level.upper else "up"

    # ── DOWN (BUY / falling WATCH) ────────────────────────────────────────
    if direction == "down":
        backtest = _load_backtest(ticker)
        if backtest is None:
            return None  # silent omission

        stats = backtest.get("levels", {}).get(level.price_str)
        if stats is None or stats.get("n", 0) == 0:
            return None  # silent omission

        df = _load_ohlc(ticker)
        if df is None or len(df) < 20:
            return None

        atr = _compute_atr(df)
        atr_floor = level.lower - atr * _ATR_MULTIPLIER

        # Backtest-derived floor (historical median drawdown)
        median_dd = stats.get("median_dd")
        has_backtest_floor = median_dd is not None and median_dd < 0
        backtest_floor = level.upper * (1 + median_dd / 100) if has_backtest_floor else None

        # Trend forecast — current momentum direction
        fc = trend_forecast(df["Close"])
        has_forecast = fc is not None and fc.confidence > 0.3

        # Blend the three signals with adaptive weights
        if has_backtest_floor and has_forecast:
            forecast_floor = fc.lower[-1]  # lower CI bound at end of forecast horizon
            # If trend is accelerating down, weight forecast higher (it sees momentum)
            # If trend is flattening/up, weight backtest higher (dip is likely shallow)
            if fc.trend_direction == "down":
                w_atr, w_bt, w_fc = 0.20, 0.35, 0.45
            elif fc.trend_direction == "up":
                w_atr, w_bt, w_fc = 0.25, 0.50, 0.25
            else:  # flat
                w_atr, w_bt, w_fc = 0.25, 0.40, 0.35
            watch = round(w_atr * atr_floor + w_bt * backtest_floor + w_fc * forecast_floor)
        elif has_backtest_floor:
            watch = round((atr_floor + backtest_floor) / 2)
        elif has_forecast:
            forecast_floor = fc.lower[-1]
            watch = round((atr_floor + forecast_floor) / 2)
        else:
            watch = round(atr_floor)

        # Sanity: watch price should be meaningfully below the level
        if watch >= level.lower:
            return None

        insufficient = stats.get("n", 0) < 3
        price_str = f"₹{watch:,.0f}"

        # Add momentum context from forecast when available
        momentum_note = ""
        if has_forecast and fc.trend_direction == "down":
            momentum_note = " — momentum down"
        elif has_forecast and fc.trend_direction == "up":
            momentum_note = " — dip may be shallow"

        # Days-to-green: "typically green in 8d" — tells the user how
        # long to be patient after buying at this level.
        median_days = stats.get("median_days")
        if median_days is not None and median_days <= 30:
            momentum_note += f", typically green in {median_days}d"

        if insufficient:
            return f"Watch for better entry near {price_str}{momentum_note} — low confidence, rebuild chart"
        return f"Watch for better entry near {price_str}{momentum_note}"

    # ── UP (SELL / rising WATCH) ──────────────────────────────────────────
    else:
        df = _load_ohlc(ticker)
        if df is None or len(df) < 20:
            return None

        atr = _compute_atr(df)
        atr_ceiling = level.upper + atr * _ATR_MULTIPLIER

        # Trend forecast for SELL direction
        fc = trend_forecast(df["Close"])
        has_forecast = fc is not None and fc.confidence > 0.3

        # Refine with nearest resistance cluster above level.upper
        resistances = _resistance_zones(df)
        candidates = [r for r in resistances if r > level.upper * 1.005]
        if candidates:
            nearest_res = min(candidates)
            if abs(nearest_res - atr_ceiling) / atr_ceiling <= _ZONE_AGREE_PCT:
                atr_ceiling = (atr_ceiling + nearest_res) / 2

        # MFE-derived ceiling: historical median peak return after hitting
        # this sell level.  Blends empirical rally data with ATR estimate.
        bt = _load_backtest(ticker)
        if bt:
            bt_stats = bt.get("levels", {}).get(level.price_str, {})
            mfe = bt_stats.get("mfe_6m")
            if mfe and mfe > 0:
                mfe_ceiling = level.upper * (1 + mfe / 100)
                atr_ceiling = (atr_ceiling + mfe_ceiling) / 2

        # Blend forecast ceiling if available
        if has_forecast:
            forecast_ceiling = fc.upper[-1]  # upper CI bound
            if fc.trend_direction == "up":
                # Momentum is with the rally — weight forecast higher
                watch = round(0.40 * atr_ceiling + 0.60 * forecast_ceiling)
            elif fc.trend_direction == "down":
                # Rally may be exhausting — conservative ceiling
                watch = round(0.65 * atr_ceiling + 0.35 * forecast_ceiling)
            else:
                watch = round(0.50 * atr_ceiling + 0.50 * forecast_ceiling)
        else:
            watch = round(atr_ceiling)

        # Sanity: ceiling should be meaningfully above the level
        if watch <= level.upper:
            return None

        price_str = f"₹{watch:,.0f}"

        # Momentum note for SELL
        if has_forecast and fc.trend_direction == "down":
            return f"Rally may be fading — trim near {price_str}"
        elif has_forecast and fc.trend_direction == "up":
            return f"Rally may extend to {price_str} — momentum still up"
        return f"Rally may extend to {price_str} before trimming"


def level_floor_summary(ticker: str) -> "list[dict] | None":
    """
    Return floor/ceiling context for all levels of a ticker.
    Used by the /backtest Telegram command.

    Returns list of dicts:
        {price_str, alert_type, signal, watch_price, hint, insufficient, n}
    or None if no backtest JSON exists.
    """
    backtest = _load_backtest(ticker)
    if backtest is None:
        return None

    df = _load_ohlc(ticker)
    atr = _compute_atr(df) if (df is not None and len(df) >= 20) else None

    # Get trend forecast once for all levels
    fc = None
    if df is not None and len(df) >= 30:
        fc = trend_forecast(df["Close"])
    has_forecast = fc is not None and fc.confidence > 0.3

    results = []
    for price_str, stats in backtest.get("levels", {}).items():
        alert_type = stats.get("alert_type", "BUY")
        n = stats.get("n", 0)
        median_dd = stats.get("median_dd")
        lower = stats.get("lower", 0.0)
        upper = stats.get("upper", 0.0)

        if n == 0 or atr is None:
            watch = None
            hint = "no data"
        elif alert_type in ("BUY", "WATCH"):
            atr_floor = lower - atr * _ATR_MULTIPLIER
            has_bt = median_dd is not None and median_dd < 0
            bt_floor = upper * (1 + median_dd / 100) if has_bt else None

            # Three-way blend when forecast is available
            if has_bt and has_forecast:
                fc_floor = fc.lower[-1]
                if fc.trend_direction == "down":
                    w_atr, w_bt, w_fc = 0.20, 0.35, 0.45
                elif fc.trend_direction == "up":
                    w_atr, w_bt, w_fc = 0.25, 0.50, 0.25
                else:
                    w_atr, w_bt, w_fc = 0.25, 0.40, 0.35
                watch = round(w_atr * atr_floor + w_bt * bt_floor + w_fc * fc_floor)
            elif has_bt:
                watch = round((atr_floor + bt_floor) / 2)
            elif has_forecast:
                watch = round((atr_floor + fc.lower[-1]) / 2)
            else:
                watch = round(atr_floor)

            insufficient = n < 3
            price_fmt = f"₹{watch:,.0f}"
            momentum = ""
            if has_forecast and fc.trend_direction == "down":
                momentum = " ↓"
            elif has_forecast and fc.trend_direction == "up":
                momentum = " ↑"
            if insufficient:
                hint = f"watch near {price_fmt}{momentum} ⚠ low confidence"
            else:
                hint = f"watch near {price_fmt}{momentum}"
        else:  # SELL
            atr_ceiling = upper + atr * _ATR_MULTIPLIER
            if df is not None:
                resistances = _resistance_zones(df)
                candidates = [r for r in resistances if r > upper * 1.005]
                if candidates:
                    nearest_res = min(candidates)
                    if abs(nearest_res - atr_ceiling) / atr_ceiling <= _ZONE_AGREE_PCT:
                        atr_ceiling = (atr_ceiling + nearest_res) / 2

            if has_forecast:
                fc_ceil = fc.upper[-1]
                if fc.trend_direction == "up":
                    watch = round(0.40 * atr_ceiling + 0.60 * fc_ceil)
                elif fc.trend_direction == "down":
                    watch = round(0.65 * atr_ceiling + 0.35 * fc_ceil)
                else:
                    watch = round(0.50 * atr_ceiling + 0.50 * fc_ceil)
            else:
                watch = round(atr_ceiling)

            momentum = ""
            if has_forecast and fc.trend_direction == "up":
                momentum = " ↑"
            elif has_forecast and fc.trend_direction == "down":
                momentum = " ↓"
            hint = f"may extend to ₹{watch:,.0f}{momentum}"

        results.append({
            "price_str":  price_str,
            "alert_type": alert_type,
            "signal":     stats.get("signal", ""),
            "watch":      watch,
            "hint":       hint,
            "n":          n,
            "insufficient": stats.get("insufficient", False),
        })

    return results
