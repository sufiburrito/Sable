"""
Multi-factor confidence scoring for live alerts.

Computes a composite confidence score at the moment an alert fires,
combining 8 independent factors. Each factor scores +1 (bullish),
0 (neutral), or -1 (bearish). The composite replaces the old static
1-5 confidence from analysis time with a live, adaptive score.

Frameworks used (implemented faithfully, not modified):
  - Stan Weinstein's Stage Analysis (30-week MA trend identification)
  - Gary Antonacci's Dual Momentum (absolute + relative momentum)
  - Mark Minervini's volume contraction (supply/demand on pullback)
  - HMM regime detection (already built in regime_context.py)
  - Backtest level strength (already built)
  - Market Mood Index (already built)
  - Insider/promoter activity (already built)

All math is in Python. No numbers computed outside code.
No network calls — everything from local caches and state files.
"""
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .ohlc_cache import read_ohlc_cache

logger = logging.getLogger(__name__)

# Where OHLC caches and backtest data live
_ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "analysis"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FactorScore:
    """One factor's contribution to the composite score."""
    name: str       # human-readable factor name
    score: int      # -1 (bearish), 0 (neutral), +1 (bullish)
    label: str      # short explanation, e.g. "Stage 2 ↑" or "RSI 28 oversold"


@dataclass
class ConfidenceResult:
    """Complete confidence assessment for one alert."""
    factors: list[FactorScore]  # all 8+ factors (9 when VCP sidecar present)
    composite: int              # sum of all scores (-13 to +13 when all Phase 2 sidecars present)
    max_score: int              # number of factors that contributed (for "X/Y" display)
    verdict: str                # "HIGH CONVICTION", "MODERATE", etc.
    emoji: str                  # 🔴, 🔵, 🟡, ⏸, 🚫
    alert_type: str = "BUY"               # needed for display logic
    # Backtest punchline — shown in the compact stats line
    expectancy: Optional[float] = None   # avg return per entry at this level (%)
    median_days: Optional[int] = None    # typical days to breakeven
    # Standing risk flag from fundamentals table (pledge > 30%)
    pledge_warning: Optional[str] = None  # e.g. "⚠️ PLEDGE 34%" or None
    # Synthesised at fire-time from factors + portfolio position
    thesis: str = ""                      # primary factual statement (line 2)
    sable_opinion: Optional[str] = None  # personal opinion, or None if nothing genuine to add
    # VCP summary line — shown in stats line when sidecar is present
    vcp_summary: Optional[str] = None    # e.g. "VCP 85 · pivot ₹782" or None


# ---------------------------------------------------------------------------
# Factor weighting (Phase 2 calibration spine)
#
# The composite was an equal-weight sum of each factor's −1/0/+1 vote. The
# calibration study (alert_bot/calibrate.py) measures each factor's predictive
# power and emits data/factor_weights.json with per-factor weights, mean-
# normalized to 1.0 so a weight-1.0 factor contributes exactly as before and
# the verdict thresholds need no change. A missing/malformed file ⇒ no weights
# ⇒ every factor defaults to weight 1.0 ⇒ byte-identical to the old equal sum.
# ---------------------------------------------------------------------------

def _load_factor_weights() -> dict[str, float]:
    """Read data/factor_weights.json. Returns {factor_name: weight}.

    Cold-start fallback: missing or malformed file → {} (all factors weight 1.0).
    """
    path = Path("data/factor_weights.json")  # relative — matches the flow/breadth/fund loaders
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        weights = data.get("weights", {})
        return {str(k): float(v) for k, v in weights.items()}
    except Exception:
        return {}


def _weighted_composite(factors: list[FactorScore], weights: dict[str, float]) -> int:
    """Weighted sum of factor scores, rounded to an int.

    A factor with no entry in `weights` (or an empty dict) defaults to weight
    1.0 — so an empty dict reproduces the plain equal-weight sum exactly.
    """
    total = sum(weights.get(f.name, 1.0) * f.score for f in factors)
    return round(total)


# ---------------------------------------------------------------------------
# OHLC loading (shared helper)
# ---------------------------------------------------------------------------

def _load_ohlc(ticker: str) -> Optional[pd.DataFrame]:
    """Load OHLC cache for a ticker. Returns None if missing/empty.

    Delegates to the shared no-fetch primitive (bean algotrading-ne2m), which
    returns a Date-INDEXED frame — previously this reader kept Date as a column.
    Downstream access is by column name / positional .iloc, so it is unaffected.
    The <30-bar guard (need enough history for moving averages) stays here.
    """
    df = read_ohlc_cache(ticker, analysis_dir=_ANALYSIS_DIR)
    if df is None or len(df) < 30:
        return None
    return df


def _load_nifty() -> Optional[pd.DataFrame]:
    """Load Nifty 50 OHLC cache for relative strength calculation.

    Returns a frame with Date as a COLUMN (not the index). The shared reader
    gives a Date-indexed frame, so we reset_index() here: external callers of
    this helper (multibagger_screener does `nifty_df.set_index("Date")`) depend
    on the Date column being present. Bean algotrading-ne2m.
    """
    df = read_ohlc_cache("NIFTY50", analysis_dir=_ANALYSIS_DIR)
    if df is None:
        return None
    return df.reset_index()


# ---------------------------------------------------------------------------
# Factor 1: Trend alignment (Weinstein Stage Analysis)
#
# Stan Weinstein's method: use the 30-week (~150-day) moving average and
# its slope to identify which of 4 stages a stock is in.
#   Stage 1 (Basing):     price near flat MA, MA slope flat
#   Stage 2 (Advancing):  price above rising MA — THE stage to buy in
#   Stage 3 (Topping):    price near flattening/rolling MA
#   Stage 4 (Declining):  price below falling MA — the falling knife zone
#
# For BUY alerts:  Stage 2 = +1, Stage 1 = 0, Stage 3/4 = -1
# For SELL alerts: Stage 3/4 = +1, Stage 2 = -1, Stage 1 = 0
# ---------------------------------------------------------------------------

def _weinstein_stage(df: pd.DataFrame) -> tuple[int, str]:
    """
    Determine the Weinstein stage from price data.
    Returns (stage_number, description).
    """
    close = df["Close"].values
    # 30-week MA ≈ 150 trading days
    if len(close) < 150:
        return 0, "insufficient data"

    ma_150 = pd.Series(close).rolling(150).mean().values
    current_price = close[-1]
    current_ma = ma_150[-1]

    # MA slope: compare current MA to MA from 20 days ago
    if np.isnan(ma_150[-20]):
        return 0, "insufficient data"
    ma_slope = (ma_150[-1] - ma_150[-20]) / ma_150[-20]

    # Thresholds for slope classification
    # Rising: slope > +0.5% over 20 days
    # Falling: slope < -0.5% over 20 days
    # Flat: in between
    price_above_ma = current_price > current_ma
    slope_rising = ma_slope > 0.005
    slope_falling = ma_slope < -0.005
    slope_flat = not slope_rising and not slope_falling

    if price_above_ma and slope_rising:
        return 2, "Stage 2 — advancing"
    elif price_above_ma and slope_flat:
        # Could be late Stage 2 or early Stage 3
        return 3, "Stage 3 — topping"
    elif not price_above_ma and slope_falling:
        return 4, "Stage 4 — declining"
    elif not price_above_ma and slope_flat:
        return 1, "Stage 1 — basing"
    elif price_above_ma and slope_falling:
        # Price above a falling MA — possible early recovery
        return 1, "Stage 1 — basing"
    else:
        # price below rising MA — pullback in uptrend
        return 2, "Stage 2 — pullback"


def _score_trend(df: pd.DataFrame, alert_type: str) -> FactorScore:
    """Score trend alignment using Weinstein Stage Analysis."""
    if df is None or len(df) < 150:
        return FactorScore("Trend", 0, "No data")

    stage, desc = _weinstein_stage(df)

    if alert_type == "BUY":
        if stage == 2:
            return FactorScore("Trend", +1, f"✓ {desc}")
        elif stage == 1:
            return FactorScore("Trend", 0, f"~ {desc}")
        else:
            return FactorScore("Trend", -1, f"✗ {desc}")
    elif alert_type == "SELL":
        if stage in (3, 4):
            return FactorScore("Trend", +1, f"✓ {desc}")
        elif stage == 1:
            return FactorScore("Trend", 0, f"~ {desc}")
        else:  # Stage 2 — selling in an uptrend, might be early
            return FactorScore("Trend", -1, f"✗ {desc}")
    else:  # WATCH
        if stage == 2:
            return FactorScore("Trend", +1, f"✓ {desc}")
        elif stage == 4:
            return FactorScore("Trend", -1, f"✗ {desc}")
        else:
            return FactorScore("Trend", 0, f"~ {desc}")


# ---------------------------------------------------------------------------
# Factor 2: Momentum (RSI)
#
# RSI measures recent price momentum on a 0-100 scale.
#   For BUY:  RSI < 35 = oversold, good entry (+1)
#             RSI 35-65 = neutral (0)
#             RSI > 65 = overbought, not ideal for buying (-1)
#   For SELL: RSI > 65 = overbought, good exit (+1)
#             RSI 35-65 = neutral (0)
#             RSI < 35 = oversold, might bounce against you (-1)
# ---------------------------------------------------------------------------

def _compute_rsi(close: np.ndarray, period: int = 14) -> float:
    """Compute the latest RSI value using Wilder's smoothing."""
    if len(close) < period + 1:
        return 50.0  # neutral default

    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Wilder's exponential moving average
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _score_momentum(df: pd.DataFrame, alert_type: str) -> FactorScore:
    """Score momentum using RSI."""
    if df is None or len(df) < 20:
        return FactorScore("Momentum", 0, "No data")

    rsi = _compute_rsi(df["Close"].values)
    rsi_str = f"RSI {rsi:.0f}"

    if alert_type == "BUY":
        if rsi < 35:
            return FactorScore("Momentum", +1, f"✓ {rsi_str} oversold")
        elif rsi > 65:
            return FactorScore("Momentum", -1, f"✗ {rsi_str} overbought")
        else:
            return FactorScore("Momentum", 0, f"~ {rsi_str}")
    elif alert_type == "SELL":
        if rsi > 65:
            return FactorScore("Momentum", +1, f"✓ {rsi_str} overbought")
        elif rsi < 35:
            return FactorScore("Momentum", -1, f"✗ {rsi_str} oversold")
        else:
            return FactorScore("Momentum", 0, f"~ {rsi_str}")
    else:  # WATCH
        if rsi < 35:
            return FactorScore("Momentum", +1, f"✓ {rsi_str} oversold")
        elif rsi > 65:
            return FactorScore("Momentum", -1, f"✗ {rsi_str} overbought")
        else:
            return FactorScore("Momentum", 0, f"~ {rsi_str}")


# ---------------------------------------------------------------------------
# Factor 3: Volume pattern (Minervini)
#
# Mark Minervini's insight: on a healthy pullback to support, volume
# should CONTRACT (sellers drying up). If volume is EXPANDING on the
# pullback, it means real selling pressure — not a buyable dip.
#
# Compare average volume over the last 5 days (the pullback) to the
# 50-day average volume. Ratio < 0.8 = contracting, > 1.2 = expanding.
# ---------------------------------------------------------------------------

def _score_volume(df: pd.DataFrame, alert_type: str) -> FactorScore:
    """Score volume pattern using Minervini's contraction principle."""
    if df is None or len(df) < 55:
        return FactorScore("Volume", 0, "No data")

    vol = df["Volume"].values
    # Recent 5-day average vs 50-day average
    recent_avg = np.mean(vol[-5:])
    baseline_avg = np.mean(vol[-50:])

    if baseline_avg == 0:
        return FactorScore("Volume", 0, "No volume data")

    ratio = recent_avg / baseline_avg

    if alert_type == "BUY":
        # Buying at support: want low volume (sellers exhausted)
        if ratio < 0.8:
            return FactorScore("Volume", +1, f"✓ Vol contracting ({ratio:.1f}x avg)")
        elif ratio > 1.3:
            return FactorScore("Volume", -1, f"✗ Vol expanding ({ratio:.1f}x avg)")
        else:
            return FactorScore("Volume", 0, f"~ Vol normal ({ratio:.1f}x avg)")
    elif alert_type == "SELL":
        # Selling at resistance: high volume = distribution (confirms sell)
        if ratio > 1.3:
            return FactorScore("Volume", +1, f"✓ Vol expanding ({ratio:.1f}x avg)")
        elif ratio < 0.8:
            return FactorScore("Volume", -1, f"✗ Vol contracting ({ratio:.1f}x avg)")
        else:
            return FactorScore("Volume", 0, f"~ Vol normal ({ratio:.1f}x avg)")
    else:  # WATCH — same as BUY (conservative)
        if ratio < 0.8:
            return FactorScore("Volume", +1, f"✓ Vol contracting ({ratio:.1f}x avg)")
        elif ratio > 1.3:
            return FactorScore("Volume", -1, f"✗ Vol expanding ({ratio:.1f}x avg)")
        else:
            return FactorScore("Volume", 0, f"~ Vol normal ({ratio:.1f}x avg)")


# ---------------------------------------------------------------------------
# Factor 4: HMM Regime
#
# Uses the already-computed regime from regime_context.py.
# The regime cache maps ticker → {"current": "bull"|"bear"|..., ...}
#
# For BUY:  bull = +1, sideways = 0, bear/volatile = -1
# For SELL: bear = +1, sideways = 0, bull = -1
# ---------------------------------------------------------------------------

def _score_regime(ticker: str, regime_cache: dict, alert_type: str) -> FactorScore:
    """Score based on HMM regime state."""
    data = regime_cache.get(ticker)
    if data is None:
        return FactorScore("Regime", 0, "No regime data")

    regime = data.get("current", "unknown")
    confidence = data.get("confidence", 0)
    conf_str = f"{confidence:.0%}" if isinstance(confidence, float) else str(confidence)

    if alert_type == "BUY":
        if regime == "bull":
            return FactorScore("Regime", +1, f"✓ Bull {conf_str}")
        elif regime == "bear":
            return FactorScore("Regime", -1, f"✗ Bear {conf_str}")
        elif regime == "volatile":
            return FactorScore("Regime", -1, f"✗ Volatile {conf_str}")
        else:
            return FactorScore("Regime", 0, f"~ Sideways {conf_str}")
    elif alert_type == "SELL":
        if regime == "bear":
            return FactorScore("Regime", +1, f"✓ Bear {conf_str}")
        elif regime == "bull":
            return FactorScore("Regime", -1, f"✗ Bull {conf_str}")
        elif regime == "volatile":
            return FactorScore("Regime", 0, f"~ Volatile {conf_str}")
        else:
            return FactorScore("Regime", 0, f"~ Sideways {conf_str}")
    else:  # WATCH — bullish bias (same as BUY)
        if regime == "bull":
            return FactorScore("Regime", +1, f"✓ Bull {conf_str}")
        elif regime == "bear":
            return FactorScore("Regime", -1, f"✗ Bear {conf_str}")
        else:
            return FactorScore("Regime", 0, f"~ {regime.title()} {conf_str}")


# ---------------------------------------------------------------------------
# Factor 5: Level strength (backtest history)
#
# How many times has this exact price level been tested, and what was
# the outcome? Levels with many touches and high win rates are stronger.
#
# n >= 5 and win_rate > 0: strong (+1)
# n >= 3: moderate (0)
# n < 3 or no data: weak (-1 for BUY, 0 for others)
# ---------------------------------------------------------------------------

def _score_level_strength(ticker: str, price_str: str, alert_type: str) -> FactorScore:
    """
    Score based on historical backtest data for this level.

    Scores on expectancy (avg profit per entry) and win rate — not just
    how many times the level was touched.  A level hit 12 times with 40%
    win rate and negative expectancy should score -1, not +1.
    """
    bt_path = _ANALYSIS_DIR / f"{ticker}_backtest.json"
    if not bt_path.exists():
        return FactorScore("Level", 0, "No backtest")

    try:
        bt = json.loads(bt_path.read_text())
        levels = bt.get("levels", {})
        stats = levels.get(price_str)

        if stats is None:
            return FactorScore("Level", 0, "Untested level")

        n = stats.get("n", 0)
        win_rate = stats.get("win_rate_6m")
        expectancy = stats.get("expectancy")

        if n < 3:
            return FactorScore("Level", 0, f"~ Only {n} tests")

        # Primary signal: expectancy (positive = profitable level)
        if expectancy is not None:
            if expectancy > 5:
                lbl = f"✓ {n}x tested, E[+{expectancy:.0f}%]"
                if win_rate and win_rate >= 70:
                    lbl = f"✓ {n}x, {win_rate}% win, E[+{expectancy:.0f}%]"
                return FactorScore("Level", +1, lbl)
            elif expectancy < -3:
                return FactorScore("Level", -1, f"✗ {n}x tested, E[{expectancy:.0f}%]")

        # Fallback to win rate when expectancy is None (not enough 6M data)
        if win_rate is not None:
            if win_rate >= 60:
                return FactorScore("Level", +1, f"✓ {n}x tested, {win_rate}% win")
            elif win_rate < 40:
                return FactorScore("Level", -1, f"✗ {n}x tested, {win_rate}% win")

        return FactorScore("Level", 0, f"~ {n}x tested")

    except (json.JSONDecodeError, OSError):
        return FactorScore("Level", 0, "Backtest error")


# ---------------------------------------------------------------------------
# Factor 6: Relative strength vs Nifty 50 (Dual Momentum — relative leg)
#
# Gary Antonacci's framework: compare the stock's performance over
# the last 3 months to Nifty 50. If the stock is outperforming the
# index, it has relative momentum — institutional support is likely.
#
# Outperforming by >5% = +1
# Within ±5% = 0
# Underperforming by >5% = -1
# ---------------------------------------------------------------------------

def _score_relative_strength(
    df: pd.DataFrame, alert_type: str, nifty: Optional[pd.DataFrame] = None
) -> FactorScore:
    """Score relative strength vs Nifty 50 over the last ~63 trading days.

    `nifty` lets a caller pass a date-aligned benchmark slice (used by the
    calibration reconstruction to avoid look-ahead). When None, the live
    Nifty cache is loaded — the production behavior, unchanged.
    """
    if nifty is None:
        nifty = _load_nifty()
    if df is None or nifty is None or len(df) < 63 or len(nifty) < 63:
        return FactorScore("RS", 0, "No benchmark data")

    # 3-month (63 trading days) return for both
    stock_ret = (df["Close"].iloc[-1] / df["Close"].iloc[-63] - 1) * 100
    nifty_ret = (nifty["Close"].iloc[-1] / nifty["Close"].iloc[-63] - 1) * 100
    excess = stock_ret - nifty_ret

    if alert_type == "BUY":
        if excess > 5:
            return FactorScore("RS", +1, f"✓ +{excess:.0f}% vs Nifty")
        elif excess < -5:
            return FactorScore("RS", -1, f"✗ {excess:.0f}% vs Nifty")
        else:
            return FactorScore("RS", 0, f"~ {excess:+.0f}% vs Nifty")
    elif alert_type == "SELL":
        # For sells: weak RS confirms the trim
        if excess < -5:
            return FactorScore("RS", +1, f"✓ {excess:.0f}% vs Nifty")
        elif excess > 5:
            return FactorScore("RS", -1, f"✗ +{excess:.0f}% vs Nifty")
        else:
            return FactorScore("RS", 0, f"~ {excess:+.0f}% vs Nifty")
    else:  # WATCH
        if excess > 5:
            return FactorScore("RS", +1, f"✓ +{excess:.0f}% vs Nifty")
        elif excess < -5:
            return FactorScore("RS", -1, f"✗ {excess:.0f}% vs Nifty")
        else:
            return FactorScore("RS", 0, f"~ {excess:+.0f}% vs Nifty")


# ---------------------------------------------------------------------------
# Factor 7: Market Mood Index (MMI)
#
# India's sentiment gauge (from TickerTape). Zones:
#   < 30:  Extreme Fear   — contrarian BUY signal
#   30-50: Fear           — leaning bullish for buys
#   50-70: Greed          — leaning bearish for buys
#   >= 70: Extreme Greed  — contrarian SELL signal
# ---------------------------------------------------------------------------

def _score_mmi(mmi_value: Optional[float], alert_type: str) -> FactorScore:
    """Score based on Market Mood Index."""
    if mmi_value is None:
        return FactorScore("MMI", 0, "No MMI data")

    mmi_str = f"MMI {mmi_value:.0f}"

    if alert_type == "BUY":
        if mmi_value < 30:
            return FactorScore("MMI", +1, f"✓ {mmi_str} Extreme Fear")
        elif mmi_value < 50:
            return FactorScore("MMI", +1, f"✓ {mmi_str} Fear")
        elif mmi_value < 70:
            return FactorScore("MMI", -1, f"✗ {mmi_str} Greed")
        else:
            return FactorScore("MMI", -1, f"✗ {mmi_str} Extreme Greed")
    elif alert_type == "SELL":
        if mmi_value >= 70:
            return FactorScore("MMI", +1, f"✓ {mmi_str} Extreme Greed")
        elif mmi_value >= 50:
            return FactorScore("MMI", +1, f"✓ {mmi_str} Greed")
        elif mmi_value >= 30:
            return FactorScore("MMI", -1, f"✗ {mmi_str} Fear")
        else:
            return FactorScore("MMI", -1, f"✗ {mmi_str} Extreme Fear")
    else:  # WATCH — same as BUY
        if mmi_value < 50:
            return FactorScore("MMI", +1, f"✓ {mmi_str} Fear zone")
        else:
            return FactorScore("MMI", -1, f"✗ {mmi_str} Greed zone")


# ---------------------------------------------------------------------------
# Factor 8: Insider / promoter activity
#
# Checks data/insider_activity.json for recent trades by insiders
# or promoters in this stock. Promoter buying near support is one of
# the strongest signals available.
#
# Net buying = +1, net selling = -1, no data = 0
# ---------------------------------------------------------------------------

def _score_insider(ticker: str, alert_type: str) -> FactorScore:
    """Score based on recent insider/promoter activity (queries market.db, falls back to JSON).

    Uses net accumulation per entity to distinguish genuine positioning from
    arbitrage round-trips. When arbitrage_ratio > 0.8, the gross signal is
    dominated by matched-pair trading and is neutralised (score = 0).
    """
    # Primary: query market.db for last 30 days
    net_data  = None
    stock_data = None
    try:
        import sys as _sys
        _sys.path.insert(0, str(_DATA_DIR.parent))
        import market_db as _mdb
        _conn = _mdb.get_conn()
        stock_data = _mdb.query_insider_summary(_conn, ticker, days=30)
        net_data   = _mdb.query_net_accumulation(_conn, ticker, days=60)
        _conn.close()
    except Exception:
        pass

    # Fallback: read insider_activity.json (pre-DB behaviour)
    if stock_data is None:
        ia_path = _DATA_DIR / "insider_activity.json"
        if not ia_path.exists():
            return FactorScore("Insider", 0, "No data")
        try:
            ia = json.loads(ia_path.read_text())
            if not ia.get("last_updated"):
                return FactorScore("Insider", 0, "No data")
            stock_data = ia.get("portfolio_activity", {}).get(ticker)
        except Exception:
            return FactorScore("Insider", 0, "No data")

    if stock_data is None:
        return FactorScore("Insider", 0, "No insider trades")

    summary          = stock_data.get("summary", {})
    net_dir          = summary.get("net_direction", "")
    promoter_buying  = summary.get("promoter_buying", False)
    promoter_selling = summary.get("promoter_selling", False)
    buy_val          = summary.get("total_buy_value_cr", 0)
    sell_val         = summary.get("total_sell_value_cr", 0)

    # Net accumulation context (may be None if market.db unavailable)
    arb_ratio   = net_data["arbitrage_ratio"]   if net_data else 0.0
    net_val     = net_data["net_value_cr"]       if net_data else (buy_val - sell_val)
    genuine_acc = net_data["genuine_accumulators"] if net_data else []

    # Arb-dominated: >80% round-trips AND no genuine accumulators — pure noise.
    # If genuine accumulators exist alongside arb activity, surface them anyway.
    arb_dominated = arb_ratio > 0.8 and not genuine_acc

    # Build a label suffix showing genuine accumulators (top 2, Telegram-safe length)
    def _acc_suffix():
        if not genuine_acc:
            return f"net ₹{net_val:.0f}Cr"
        names = [f"{a['party_name'].split()[0]} ₹{a['net_cr']:.0f}Cr" for a in genuine_acc[:2]]
        suffix = " · ".join(names)
        if len(genuine_acc) > 2:
            suffix += f" +{len(genuine_acc) - 2} more"
        return suffix

    if alert_type == "BUY":
        if arb_dominated:
            return FactorScore("Insider", 0, f"~ Bulk arb {arb_ratio:.0%} · net ₹{net_val:.0f}Cr")
        if promoter_buying:
            return FactorScore("Insider", +1, f"✓ Promoter buying · {_acc_suffix()}")
        elif net_dir == "buy":
            return FactorScore("Insider", +1, f"✓ Smart money · {_acc_suffix()}")
        elif promoter_selling:
            return FactorScore("Insider", -1, f"✗ Promoter selling ₹{sell_val:.0f}Cr")
        elif net_dir == "sell":
            return FactorScore("Insider", -1, f"✗ Insider selling ₹{sell_val:.0f}Cr")
        else:
            return FactorScore("Insider", 0, "No recent insider trades")
    elif alert_type == "SELL":
        if arb_dominated:
            return FactorScore("Insider", 0, f"~ Bulk arb {arb_ratio:.0%} · net ₹{net_val:.0f}Cr")
        if promoter_selling or net_dir == "sell":
            return FactorScore("Insider", +1, f"✓ Insider selling ₹{sell_val:.0f}Cr")
        elif promoter_buying or net_dir == "buy":
            return FactorScore("Insider", -1, f"✗ Smart money · {_acc_suffix()}")
        else:
            return FactorScore("Insider", 0, "No recent insider trades")
    else:  # WATCH — same logic as BUY
        if arb_dominated:
            return FactorScore("Insider", 0, f"~ Bulk arb {arb_ratio:.0%} · net ₹{net_val:.0f}Cr")
        if promoter_buying or net_dir == "buy":
            return FactorScore("Insider", +1, f"✓ Smart money · {_acc_suffix()}")
        elif promoter_selling or net_dir == "sell":
            return FactorScore("Insider", -1, f"✗ Insider selling ₹{sell_val:.0f}Cr")
        else:
            return FactorScore("Insider", 0, "No recent insider trades")


# ---------------------------------------------------------------------------
# Factor 9: VCP composite (Volatility Contraction Pattern)
#
# Reads the nightly-written analysis/{TICKER}_vcp.json sidecar.
# Never called live — the sidecar is the data source to keep the hot
# alert path free of heavy computation.
#
# BUY:  high VCP score (≥80) = clean Stage 2 coil → +1 (setup confirmed)
#       moderate (50-79) = neutral setup → 0
#       low (<50) = weak structure → -1
# SELL: high VCP score = breakout still building → -1 (don't sell the coil)
# WATCH: always 0 (VCP is a directional signal)
# ---------------------------------------------------------------------------

def _score_vcp(ticker: str, alert_type: str, vcp_data: dict | None = None) -> FactorScore:
    """
    Factor 9 — VCP composite. Accepts pre-loaded vcp_data to avoid double read
    when compute_confidence() has already loaded the sidecar for vcp_summary.

    When vcp_data is None, attempts to read analysis/{ticker}_vcp.json.
    Returns a neutral score (0) with label "VCP:n/a" when the sidecar is absent.
    """
    if vcp_data is None:
        sidecar = Path(f"analysis/{ticker}_vcp.json")
        if not sidecar.exists():
            return FactorScore("vcp", 0, "VCP:n/a")
        try:
            vcp_data = json.loads(sidecar.read_text())
            if vcp_data is None:
                return FactorScore("vcp", 0, "VCP:err")
        except (json.JSONDecodeError, OSError):
            return FactorScore("vcp", 0, "VCP:err")

    composite = vcp_data.get("composite_score", 0.0)
    is_vcp    = vcp_data.get("is_vcp", False)
    # Import here to avoid circular import — vcp_scorer imports nothing from confidence
    from alert_bot.vcp_scorer import score_factor
    result = score_factor(composite, is_vcp, alert_type)
    return FactorScore("vcp", result.score, result.label)


# ---------------------------------------------------------------------------
# Factor 10: India VIX regime
#
# Per docs/fno_signals.md §1 — signal-only. No Black-Scholes, no Greeks.
# VIX informs delivery timing only.
#
# BUY:  VIX 25-35 (fear/high fear) = contrarian accumulation zone → +1
#       VIX >35 (crisis)           = cash heavy, wait for VIX<30 → -1
#       VIX <12 (extreme complacency) = skeptical of breakouts → -1
#       VIX 12-25 (normal/elevated) = neutral → 0
#
# SELL: VIX >25 (fear) = don't sell into the bottom, likely near support → -1
#       VIX <12 (complacency) = good time to trim → +1
#       VIX 12-25 = neutral → 0
#
# WATCH: always 0 (VIX is a directional timing signal, not a watch trigger)
# ---------------------------------------------------------------------------

def _score_vix(alert_type: str) -> FactorScore:
    """
    Factor 10 — India VIX regime. Per docs/fno_signals.md §1.

    BUY: VIX 25-35 (high fear) = contrarian +1; VIX >35 (crisis) = -1; VIX <12 = -1.
    SELL: VIX >25 (fear) = -1 (don't sell into bottom); VIX <12 (complacency) = +1.
    WATCH: always 0.
    """
    path = Path("data/fno_signals.json")
    if not path.exists():
        return FactorScore("vix", 0, "VIX:n/a")
    try:
        data = json.loads(path.read_text())
        vix = data.get("vix", {})
        if isinstance(vix, dict):
            vix = vix.get("value")
        else:
            vix = None
    except (json.JSONDecodeError, OSError):
        return FactorScore("vix", 0, "VIX:err")

    if vix is None:
        return FactorScore("vix", 0, "VIX:n/a")

    label = f"VIX:{vix:.1f}"
    if alert_type == "WATCH":
        return FactorScore("vix", 0, label)

    if alert_type == "BUY":
        if 25 <= vix <= 35: return FactorScore("vix",  1, f"{label}↑fear")
        if vix > 35:        return FactorScore("vix", -1, f"{label}crisis")
        if vix < 12:        return FactorScore("vix", -1, f"{label}complacent")
        return FactorScore("vix", 0, label)
    else:  # SELL
        if vix > 25: return FactorScore("vix", -1, f"{label}↑fear")
        if vix < 12: return FactorScore("vix",  1, f"{label}complacent")
        return FactorScore("vix", 0, label)


# ---------------------------------------------------------------------------
# Factor 11: FII/DII institutional flow regime
#
# Per docs/fii_dii_methodology.md — the 6-regime classifier.
# Data source: data/flow_regime.json (written by fetch_fii_dii.py).
#
# BUY:  DUAL_BUYING / NET_BUYER / DII_ABSORPTION → +1 (institutional tailwind
#       or contrarian absorption); NET_SELLER / DUAL_SELLING → -1.
# SELL: NET_SELLER / DUAL_SELLING → +1 (confirmed weak market, sell into it);
#       DUAL_BUYING / NET_BUYER → -1 (selling into strong flow = fighting tape).
# WATCH: always 0.
# ---------------------------------------------------------------------------

def _score_flow_regime(alert_type: str) -> FactorScore:
    """
    Factor 11 — FII/DII market flow regime. Per docs/fii_dii_methodology.md.

    BUY:  DUAL_BUYING / NET_BUYER / DII_ABSORPTION → +1 (institutional tailwind or
          contrarian absorption); NET_SELLER / DUAL_SELLING → -1.
    SELL: NET_SELLER / DUAL_SELLING → +1 (selling into weak market = confirmed);
          DUAL_BUYING / NET_BUYER → -1 (selling into strong flow = fighting tape).
    """
    path = Path("data/flow_regime.json")
    if not path.exists():
        return FactorScore("flow", 0, "FLOW:n/a")
    try:
        data   = json.loads(path.read_text())
        regime = data.get("regime", "TRANSITION")
        streak = data.get("streak_days", 0)
    except (json.JSONDecodeError, OSError):
        return FactorScore("flow", 0, "FLOW:err")

    BUY_MAP  = {"DUAL_BUYING": 1, "NET_BUYER": 1, "DII_ABSORPTION": 1,
                "TRANSITION": 0, "NET_SELLER": -1, "DUAL_SELLING": -1}
    SELL_MAP = {"DUAL_BUYING": -1, "NET_BUYER": -1, "DII_ABSORPTION": 0,
                "TRANSITION": 0, "NET_SELLER": 1, "DUAL_SELLING": 1}

    score = (BUY_MAP if alert_type == "BUY" else
             SELL_MAP if alert_type == "SELL" else {}).get(regime, 0)
    label = f"FLOW:{regime[:4]}" + (f"×{streak}d" if streak > 1 else "")
    return FactorScore("flow", score, label)


# ---------------------------------------------------------------------------
# Factor 12: Market breadth zone
#
# Per docs/market_breadth_methodology.md — 5-component health score.
# Data source: data/breadth.json (written by market breadth pipeline).
#
# BUY:  STRONG / HEALTHY → +1 (broad participation); WEAKENING / CRITICAL → -1.
# SELL: WEAKENING / CRITICAL → +1 (confirmed weak market); STRONG / HEALTHY → -1.
# WATCH: always 0 (breadth is a directional filter, not a watch trigger).
# ---------------------------------------------------------------------------

def _score_breadth(alert_type: str) -> FactorScore:
    """
    Factor 12 — Market breadth zone. Per docs/market_breadth_methodology.md.

    BUY:  STRONG/HEALTHY → +1 (broad market participation); WEAKENING/CRITICAL → -1.
    SELL: WEAKENING/CRITICAL → +1 (confirmed weak market); STRONG/HEALTHY → -1.
    WATCH: always 0.
    """
    path = Path("data/breadth.json")
    if not path.exists():
        return FactorScore("breadth", 0, "BDT:n/a")
    try:
        zone = json.loads(path.read_text()).get("zone")
    except (json.JSONDecodeError, OSError):
        return FactorScore("breadth", 0, "BDT:err")

    if alert_type == "WATCH" or zone is None:
        return FactorScore("breadth", 0, f"BDT:{zone or 'n/a'}")

    if alert_type == "BUY":
        score = 1 if zone in ("STRONG", "HEALTHY") else (-1 if zone in ("WEAKENING", "CRITICAL") else 0)
    else:  # SELL
        score = 1 if zone in ("WEAKENING", "CRITICAL") else (-1 if zone in ("STRONG", "HEALTHY") else 0)

    return FactorScore("breadth", score, f"BDT:{zone}")


# ---------------------------------------------------------------------------
# Factor 13: Fundamental quality score
#
# Reads analysis/{TICKER}_fund_score.json (written by nightly analysis).
# A quality compounder at support should be bought; a weak company at
# resistance should be trimmed without hesitation.
#
# BUY:  score ≥ 7.5 → +1 (quality at support); score < 5.0 → -1.
# SELL: score ≥ 7.5 → -1 (don't sell a quality compounder); score ≤ 4.0 → +1.
# WATCH: always 0.
# ---------------------------------------------------------------------------

def _score_fundamental(ticker: str, alert_type: str) -> FactorScore:
    """
    Factor 13 — Fundamental quality score. Reads analysis/{TICKER}_fund_score.json.

    BUY:  score ≥ 7.5 → +1 (quality company at support);  score < 5.0 → -1.
    SELL: score ≥ 7.5 → -1 (don't sell a quality compounder); score ≤ 4.0 → +1.
    WATCH: always 0.
    """
    path = Path(f"analysis/{ticker}_fund_score.json")
    if not path.exists():
        return FactorScore("fund", 0, "FUND:n/a")
    try:
        score_val = json.loads(path.read_text()).get("score", 5.0)
    except (json.JSONDecodeError, OSError):
        return FactorScore("fund", 0, "FUND:err")

    label = f"FUND:{score_val:.1f}"
    if alert_type == "WATCH":
        return FactorScore("fund", 0, label)

    if alert_type == "BUY":
        if score_val >= 7.5:   return FactorScore("fund",  1, label)
        elif score_val < 5.0:  return FactorScore("fund", -1, label)
        return FactorScore("fund", 0, label)
    else:  # SELL
        if score_val >= 7.5:   return FactorScore("fund", -1, label)
        elif score_val <= 4.0: return FactorScore("fund",  1, label)
        return FactorScore("fund", 0, label)


# ---------------------------------------------------------------------------
# DMA factors (14, 15, 16) — daily-moving-average support, mean-reversion,
# and regime-flip. Hot-path safe: pure OHLC, no network. Each fills an
# information dimension the other factors don't cover:
#   - support/resistance proximity to the institutionally-defended 200/50-DMA
#   - mean-reversion (price stretch vs the 200-DMA)
#   - regime flip (a *recent* 50×200 cross, an event — not the static VCP stack)
# Neutral / no-signal / no-data readings carry a ":n/a" label so they are
# excluded from the has_data conviction denominator — they only count when
# they actually cast a vote (consistent with the VCP / VIX / flow factors).
# ---------------------------------------------------------------------------

# Proximity band for "near a DMA" — matches floor_context._ZONE_CLUSTER_PCT.
_DMA_PROXIMITY = 0.025


def _zscore_vs_ma(df: pd.DataFrame, window: int) -> Optional[float]:
    """
    Z-score of the latest close vs its trailing-`window` mean.
    Mirrors gold.py:_zscore_vs_ma so the two stay behaviourally identical.
    Returns None if there is not enough history or the window is flat.
    """
    if df is None or len(df) < window:
        return None
    closes = df["Close"].iloc[-window:]
    ma = closes.mean()
    sd = closes.std()
    if sd == 0 or pd.isna(sd):
        return None
    return float((float(df["Close"].iloc[-1]) - ma) / sd)


def _ma_slope(ma: np.ndarray, lookback: int = 20) -> float:
    """20-day slope of a moving-average series, as a fraction. 0.0 if unknown."""
    if len(ma) <= lookback or np.isnan(ma[-1]) or np.isnan(ma[-1 - lookback]):
        return 0.0
    base = ma[-1 - lookback]
    if base == 0:
        return 0.0
    return float((ma[-1] - base) / base)


def _score_dma_support(df: pd.DataFrame, alert_type: str) -> FactorScore:
    """
    Factor 14 — proximity to a daily-MA acting as dynamic support/resistance.

    BUY/WATCH: +1 if price is within 2.5% above a *rising* 200- or 50-DMA
               (the alert level coincides with defended institutional support —
               the rising 200-DMA is where the DII SIP-floor physically buys).
               -1 if price is below a *falling* 200-DMA (support has failed;
               Weinstein falling-knife). Else neutral.
    SELL:      +1 if price sits within 2.5% below the 200-DMA (overhead
               resistance the rally is fighting). Else neutral.
    """
    # 220 rows, not 200: the BUY/WATCH slope reads ma200[-21], which needs a
    # valid 200-bar mean 20 bars back. Below 220 the slope is always NaN→0 and
    # the factor would silently never vote.
    if df is None or len(df) < 220:
        return FactorScore("DMA-S", 0, "DMA:n/a")

    close = df["Close"].values
    price = float(close[-1])
    ma200 = pd.Series(close).rolling(200).mean().values
    ma50 = pd.Series(close).rolling(50).mean().values
    m200 = float(ma200[-1])
    m50 = float(ma50[-1])
    band = _DMA_PROXIMITY

    if alert_type == "SELL":
        # Resistance: price approaching the 200-DMA from underneath.
        if m200 * (1 - band) <= price <= m200:
            return FactorScore("DMA-S", 1, "below 200-DMA resistance")
        return FactorScore("DMA-S", 0, "DMA:n/a")

    # BUY / WATCH — support-seeking.
    rising200 = _ma_slope(ma200) > 0.005
    rising50 = _ma_slope(ma50) > 0.005
    falling200 = _ma_slope(ma200) < -0.005

    near_rising_200 = rising200 and m200 <= price <= m200 * (1 + band)
    near_rising_50 = rising50 and m50 <= price <= m50 * (1 + band)
    if near_rising_200:
        return FactorScore("DMA-S", 1, "on rising 200-DMA")
    if near_rising_50:
        return FactorScore("DMA-S", 1, "on rising 50-DMA")
    if falling200 and price < m200:
        return FactorScore("DMA-S", -1, "below falling 200-DMA")
    return FactorScore("DMA-S", 0, "DMA:n/a")


def _score_dma_extension(df: pd.DataFrame, alert_type: str) -> FactorScore:
    """
    Factor 15 — mean-reversion: how stretched price is vs the 200-DMA, in σ.

    BUY/WATCH: +1 when z <= -1.5 (oversold snap-back tailwind).
    SELL:      +1 when z >= +2.0 (stretched — trim into extension; the same σ
               threshold gold.py uses to gate lump-sum buys).
    WATCH also votes -1 at the overbought extreme.
    """
    z = _zscore_vs_ma(df, 200)
    if z is None:
        return FactorScore("DMA-X", 0, "DMA:n/a")

    label = f"{z:+.1f}σ vs 200-DMA"
    if alert_type == "SELL":
        if z >= 2.0:
            return FactorScore("DMA-X", 1, label)
        return FactorScore("DMA-X", 0, "DMA:n/a")

    # BUY / WATCH.
    if z <= -1.5:
        return FactorScore("DMA-X", 1, label)
    if alert_type == "WATCH" and z >= 2.0:
        return FactorScore("DMA-X", -1, label)
    return FactorScore("DMA-X", 0, "DMA:n/a")


def _score_dma_cross(df: pd.DataFrame, alert_type: str) -> FactorScore:
    """
    Factor 16 — regime flip: a *recent* 50×200 crossover (golden/death cross).

    Designed as a recent-event signal (sign change within the last ~20 bars),
    not a static stack state, to stay orthogonal to the VCP trend template.

    BUY/WATCH: +1 on a recent golden cross, -1 on a recent death cross.
    SELL:      inverse.
    """
    if df is None or len(df) < 220:
        return FactorScore("DMA-C", 0, "DMA:n/a")

    close = df["Close"].values
    ma50 = pd.Series(close).rolling(50).mean().values
    ma200 = pd.Series(close).rolling(200).mean().values
    if (np.isnan(ma200[-1]) or np.isnan(ma200[-21])
            or np.isnan(ma50[-1]) or np.isnan(ma50[-21])):
        return FactorScore("DMA-C", 0, "DMA:n/a")

    now = ma50[-1] - ma200[-1]
    past = ma50[-21] - ma200[-21]
    buy_side = alert_type in ("BUY", "WATCH")

    if past <= 0 and now > 0:        # golden cross within the window
        return FactorScore("DMA-C", 1 if buy_side else -1, "golden cross")
    if past >= 0 and now < 0:        # death cross within the window
        return FactorScore("DMA-C", -1 if buy_side else 1, "death cross")
    return FactorScore("DMA-C", 0, "DMA:n/a")


def _format_dma_hint(curr_price: float, ma200: float, slope: float) -> Optional[str]:
    """
    Build the one-line DMA enrichment string, or None when price is not near
    the 200-DMA. Pure formatter (no I/O) so it's unit-testable on its own.
    Silent-omit beyond the proximity band keeps the alert clean.
    """
    if ma200 <= 0:
        return None
    pct = (curr_price - ma200) / ma200 * 100
    if abs(pct) > _DMA_PROXIMITY * 100:        # >2.5% away → not "near" the DMA
        return None
    side = "above" if pct >= 0 else "below"
    # The "why" clause is keyed on trend, since the meaning flips: a rising
    # 200-DMA is institutionally-defended support (India's monthly SIP flows buy
    # there), a falling one is overhead resistance.
    if slope > 0.005:
        trend, why = "rising", "the long-term line where institutional SIP flows defend price"
    elif slope < -0.005:
        trend, why = "falling", "a falling long-term line caps rallies; it's resistance, not a floor"
    else:
        trend, why = "flat", "a flat long-term line; trend unresolved"
    return (f"₹{curr_price:,.0f} sits {abs(pct):.1f}% {side} a "
            f"{trend} 200-DMA (₹{ma200:,.0f}) — {why}")


def dma_hint(ticker: str, curr_price: float) -> Optional[str]:
    """
    Telegram enrichment line naming the 200-DMA when the alert price is within
    2.5% of it — the institutionally-defended line (rising 200-DMA = DII SIP
    floor). Hot-path safe: reads only the cached OHLC, no network. Returns None
    (silent omission) when there's no cache, too little history, or price is
    clear of the DMA.
    """
    df = _load_ohlc(ticker)
    if df is None or len(df) < 220:
        return None
    ma200 = pd.Series(df["Close"].values).rolling(200).mean().values
    if np.isnan(ma200[-1]) or np.isnan(ma200[-21]):
        return None
    return _format_dma_hint(curr_price, float(ma200[-1]), _ma_slope(ma200))


def _check_pledge_risk(ticker: str) -> Optional[str]:
    """
    Return '⚠️ PLEDGE X%' if promoter pledge > 30% in the most recent
    fundamentals row, else None. This is a standing risk flag — pledge above
    30% creates forced-selling vulnerability at market lows regardless of
    other confidence factors.
    """
    try:
        import market_db as _mdb
        conn = _mdb.get_conn()
        row = conn.execute("""
            SELECT promoter_pledge_pct FROM fundamentals
            WHERE ticker = ? AND promoter_pledge_pct IS NOT NULL
            ORDER BY period DESC LIMIT 1
        """, (ticker,)).fetchone()
        conn.close()
        if row and row["promoter_pledge_pct"] > 30:
            return f"⚠️ PLEDGE {row['promoter_pledge_pct']:.0f}%"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Thesis and Sable opinion synthesis
# ---------------------------------------------------------------------------

def _synthesize_thesis(
    ticker: str,
    alert_type: str,
    factors: list,
    pledge_warning: Optional[str],
    position: Optional[dict],
) -> str:
    """
    Build the thesis line (line 2 of the alert) — one factual sentence.

    Portfolio state is the primary input when a position exists; signal factors
    are primary when there is no position. Pledge overrides all when critical.
    """
    # Critical pledge overrides everything
    if pledge_warning and "PLEDGE" in pledge_warning:
        try:
            pct = float(pledge_warning.split("PLEDGE")[1].strip().rstrip("%"))
            if pct > 40:
                return f"Pledged {pct:.0f}% of holdings — forced-selling risk active at market lows. Reduce size."
        except (ValueError, IndexError):
            pass

    insider_factor = factors[7]  # _score_insider is factor index 7
    trend_factor   = factors[0]
    volume_factor  = factors[2]
    regime_factor  = factors[3]
    momentum_factor = factors[1]

    def _insider_name() -> str:
        """Extract first accumulator name from insider factor label."""
        label = insider_factor.label
        if "Smart money" in label and "·" in label:
            # Label format: "✓ Smart money · BNP ₹39Cr · SOCIETE ₹17Cr +1 more"
            parts = label.split("·")
            if len(parts) > 1:
                return parts[1].strip().split("₹")[0].strip()
        return "Smart money"

    def _pledge_suffix() -> str:
        if pledge_warning:
            try:
                pct = float(pledge_warning.split("PLEDGE")[1].strip().rstrip("%"))
                return f" · Pledged {pct:.0f}% — monitor in drawdowns."
            except (ValueError, IndexError):
                pass
        return ""

    if position is None:
        # No existing position — thesis is about the signal quality
        if insider_factor.score > 0 and "Smart money" in insider_factor.label:
            name = _insider_name()
            return f"{name} entered here — genuine institutional accumulation with no matching sell.{_pledge_suffix()}"
        if trend_factor.score > 0 and volume_factor.score > 0:
            return f"Stage 2 advance confirmed on volume — momentum is institutionally backed.{_pledge_suffix()}"
        n_positive = sum(1 for f in factors if f.score > 0)
        if n_positive >= 3:
            return f"{n_positive} signals aligned. No position yet — size conservatively for the first tranche.{_pledge_suffix()}"
        return f"Level reached. No position held — verify thesis in stocks/{ticker}.md before opening.{_pledge_suffix()}"
    else:
        # Existing position — thesis tells the user what their position means right now
        qty = position.get("quantity", 0)
        avg = position.get("avg_buy_price", 0.0)

        # Get core % from stocks/TICKER.md (re-use portfolio_context helper)
        try:
            from alert_bot.portfolio_context import _core_pct
            core_pct = _core_pct(ticker)
        except Exception:
            core_pct = 0

        core_qty  = round(qty * core_pct / 100)
        swing_qty = qty - core_qty

        # Thesis is the SIGNAL/meaning only — the position numbers (qty @ avg, P&L)
        # render on the 📊 context line, so don't repeat them here.
        if alert_type == "SELL":
            if swing_qty > 0:
                return f"Take swing profit here at resistance — core of {core_qty} shares holds.{_pledge_suffix()}"
            else:
                return f"Core-only — don't trim; only exit if thesis breaks.{_pledge_suffix()}"
        else:  # BUY / WATCH
            if swing_qty <= 0 and core_pct > 0:
                return f"Position full — no add needed; watch for a pullback to reload swing.{_pledge_suffix()}"
            if swing_qty > 0:
                return f"Adding here extends your swing layer.{_pledge_suffix()}"
            return f"Building position — {insider_factor.label if insider_factor.score > 0 else 'thesis unchanged'}.{_pledge_suffix()}"


def _synthesize_sable_opinion(
    ticker: str,
    alert_type: str,
    factors: list,
    verdict: str,
    position: Optional[dict],
    pledge_warning: Optional[str],
    regime_current: str = "",
) -> Optional[str]:
    """
    Sable's personal opinion — one sentence in first person, or None.

    Fires only when there is something genuinely worth adding that the thesis
    line doesn't already say. May contradict the computed verdict.
    """
    insider_factor  = factors[7]
    trend_factor    = factors[0]
    regime_factor   = factors[3]
    n_positive      = sum(1 for f in factors if f.score > 0)
    n_negative      = sum(1 for f in factors if f.score < 0)
    is_bear_regime  = regime_current.lower() in ("bear", "volatile")
    is_bull_regime  = regime_current.lower() == "bull"

    # 1. Strong insider + high conviction — affirm with warmth
    if insider_factor.score > 0 and verdict in ("HIGH CONVICTION",) and is_bull_regime:
        return (
            "This one feels genuinely right. When smart money and chart structure "
            "both say yes at the same level, that's not a coincidence — it's a setup."
        )

    # 2. High conviction verdict but bear/volatile regime — voice the tension honestly
    if verdict in ("HIGH CONVICTION", "MODERATE") and is_bear_regime and alert_type == "BUY":
        return (
            f"I'm cautious despite the score. The regime is {regime_current.lower()} — "
            "even good setups take longer to pay off when the tide is against you. "
            "Start very small if you must."
        )

    # 3. Pledge risk — texture beyond the mechanical warning
    if pledge_warning and "PLEDGE" in pledge_warning:
        try:
            pct = float(pledge_warning.split("PLEDGE")[1].strip().rstrip("%"))
            if pct > 40:
                return (
                    f"The signal is real, but a {pct:.0f}% pledge is a sword hanging over this. "
                    "Size it like you're comfortable losing it all in a margin-call scenario."
                )
            elif pct > 20:
                return (
                    f"Pledge at {pct:.0f}% is elevated but not critical yet. "
                    "Keep position small and watch for any increase in pledge ratio."
                )
        except (ValueError, IndexError):
            pass

    # 4. SELL alert on a winning position — affirm the exit clearly
    if alert_type == "SELL" and position:
        return "Take it. Resistance zones exist to be respected, and you've earned this one."

    # 5. Multiple factors against a BUY alert — voice the doubt
    if alert_type == "BUY" and n_negative >= 3:
        return (
            "I wouldn't act on this yet. Too many factors are working against it — "
            "wait for the signal to clean up before committing capital."
        )

    # 6. WATCH with a clear directional lean
    if alert_type == "WATCH" and n_positive >= 4:
        return "The lean here is constructive — if this resolves upward, it's worth a first tranche."
    if alert_type == "WATCH" and n_negative >= 3:
        return "Leave this alone for now. The weight of evidence is against it."

    # 7. Insider buying but weak overall score — note the conflict
    if insider_factor.score > 0 and n_positive < 3 and alert_type == "BUY":
        return (
            "The institutional buy is the only strong signal here. "
            "That's worth respecting, but don't size it like a full conviction entry."
        )

    # Nothing genuine to add — stay silent
    return None


# ---------------------------------------------------------------------------
# Composite scoring and verdict
# ---------------------------------------------------------------------------

def _composite_verdict(
    composite: int, n_factors: int, alert_type: str, insider_positive: bool = False
) -> tuple[str, str]:
    """
    Map composite score to a verdict. Thresholds are percentage-based so the
    verdict scales correctly as factors are added (8 → 13).

    At n_factors=8:  hi=6, mid=4, lo=2  (identical to old hardcoded values)
    At n_factors=13: hi=10, mid=7, lo=4  (appropriate scaling)

    Emoji is a single STRENGTH axis (green = strongest signal, red = weakest), the
    same for BUY and SELL — direction is already in the ACTION word. The verdict
    STRINGS are unchanged (they feed _sizing_hint and the display tier map).

      ≥75%  → 🟢   ≥50% → 🟡   ≥25% → 🟠   <25% → 🔴
    """
    hi  = math.ceil(n_factors * 0.75)
    mid = math.ceil(n_factors * 0.50)
    lo  = math.ceil(n_factors * 0.25)

    if alert_type == "SELL":
        if composite >= hi:    return "STRONG SELL",    "🟢"
        elif composite >= mid: return "CONFIRMED SELL", "🟡"
        elif composite >= lo:  return "MODERATE SELL",  "🟠"
        else:                  return "WEAK SELL",      "🔴"
    else:  # BUY / WATCH
        if composite >= hi:    return "HIGH CONVICTION", "🟢"
        elif composite >= mid: return "MODERATE",        "🟡"
        elif composite >= lo:  return "BUILDING",        "🟠"
        else:                  return "WEAK",            "🔴"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_confidence(
    alert_type: str,
    ticker: str,
    price_str: str,
    curr_price: float,
    regime_cache: dict,
    mmi_value: Optional[float],
) -> ConfidenceResult:
    """
    Compute the multi-factor confidence score for a fired alert.

    Called from main.py during the alert dispatch loop, immediately
    after crossing detection. All data comes from local caches —
    no network calls, no blocking.

    Parameters:
      alert_type:    "BUY", "SELL", or "WATCH"
      ticker:        NSE ticker (e.g. "SUVEN")
      price_str:     alert level string (e.g. "₹200-204") for backtest lookup
      curr_price:    current market price
      regime_cache:  dict from compute_all_regimes() (ticker → regime data)
      mmi_value:     latest MMI value from state (can be None)

    Returns:
      ConfidenceResult with per-factor scores, composite, and verdict.
    """
    # Load OHLC once — shared across multiple factors
    df = _load_ohlc(ticker)

    # Load VCP sidecar once — shared by Factor 9 (_score_vcp) and vcp_summary display.
    # A single file read here prevents a double read inside _score_vcp.
    vcp_data = None
    _vcp_path = Path(f"analysis/{ticker}_vcp.json")
    if _vcp_path.exists():
        try:
            vcp_data = json.loads(_vcp_path.read_text())
        except Exception:
            pass

    factors = [
        _score_trend(df, alert_type),           # Factor 1
        _score_momentum(df, alert_type),         # Factor 2
        _score_volume(df, alert_type),           # Factor 3
        _score_regime(ticker, regime_cache, alert_type),  # Factor 4
        _score_level_strength(ticker, price_str, alert_type),  # Factor 5
        _score_relative_strength(df, alert_type),  # Factor 6
        _score_mmi(mmi_value, alert_type),       # Factor 7
        _score_insider(ticker, alert_type),      # Factor 8
        _score_vcp(ticker, alert_type, vcp_data),  # Factor 9 (neutral when sidecar absent)
        _score_vix(alert_type),                  # Factor 10 (neutral when fno_signals.json absent)
        _score_flow_regime(alert_type),          # 11 — FII/DII institutional flow
        _score_breadth(alert_type),              # 12 — market breadth zone
        _score_fundamental(ticker, alert_type),  # 13 — fundamental quality
        _score_dma_support(df, alert_type),      # 14 — DMA support/resistance proximity
        _score_dma_extension(df, alert_type),    # 15 — mean-reversion vs 200-DMA
        _score_dma_cross(df, alert_type),        # 16 — recent 50×200 golden/death cross
    ]

    # Only count factors that had data (score != 0 OR label doesn't say "No")
    # All factors contribute to the composite, but max_score reflects how many
    # had actual data (for the "X/Y factors aligned" display).
    has_data = [f for f in factors if ":n/a" not in f.label and ":err" not in f.label
                and "No " not in f.label and "error" not in f.label.lower()]
    composite = _weighted_composite(factors, _load_factor_weights())
    n_factors = len(has_data) if has_data else len(factors)

    insider_positive = factors[7].score > 0
    verdict, emoji = _composite_verdict(composite, n_factors, alert_type, insider_positive)

    # Build vcp_summary from the same loaded data (single read, no extra I/O).
    vcp_summary = None
    if vcp_data:
        cs = vcp_data.get("composite_score", 0)
        pv = vcp_data.get("pivot")
        vcp_summary = (f"VCP {cs:.0f} ({_vcp_gloss(cs, alert_type)})"
                       + (f" · pivot ₹{pv:.0f}" if pv else ""))

    # Load backtest punchline for the compact display line.
    # These are the two numbers that add information beyond the verdict:
    # expectancy ("avg +12% in 6mo") and days-to-green ("green in ~8d").
    bt_expectancy = None
    bt_median_days = None
    bt_path = _ANALYSIS_DIR / f"{ticker}_backtest.json"
    if bt_path.exists():
        try:
            bt = json.loads(bt_path.read_text())
            bt_stats = bt.get("levels", {}).get(price_str, {})
            bt_expectancy = bt_stats.get("expectancy")
            bt_median_days = bt_stats.get("median_days")
        except (json.JSONDecodeError, OSError):
            pass

    # Standing risk flag + portfolio position — used by synthesis functions.
    pledge_warning = _check_pledge_risk(ticker)
    regime_current = regime_cache.get(ticker, {}).get("current", "")
    position = None
    try:
        from alert_bot.portfolio_context import _get_position
        position = _get_position(ticker)
    except Exception:
        pass

    thesis = _synthesize_thesis(ticker, alert_type, factors, pledge_warning, position)
    sable_opinion = _synthesize_sable_opinion(
        ticker, alert_type, factors, verdict, position, pledge_warning, regime_current
    )

    return ConfidenceResult(
        factors=factors,
        composite=composite,
        max_score=n_factors,
        verdict=verdict,
        emoji=emoji,
        alert_type=alert_type,
        expectancy=bt_expectancy,
        median_days=bt_median_days,
        pledge_warning=pledge_warning,
        thesis=thesis,
        sable_opinion=sable_opinion,
        vcp_summary=vcp_summary,
    )


def _vcp_gloss(score: float, alert_type: str) -> str:
    """
    Translate a VCP composite score (0-100) into a plain *action* phrase for the
    alert line — not a chart-state word.

    A VCP (Volatility Contraction Pattern) score measures how tightly a stock's
    price has coiled: high = swings have shrunk to a tight band, so a sharp move
    (usually upward in a Stage 2 setup) may be near; low = still loose and choppy.
    The user reads this for *timing*, so the phrase says what to do, and flips by
    direction: on a BUY/WATCH it's about entering, on a SELL it's about trimming.

    Bands mirror the factor thresholds in _score_vcp (≥80 strong / 50-79 neutral).
    """
    if alert_type == "SELL":
        # A tight coil on a SELL means the up-move may still be building — don't
        # rush the trim. A loose chart gives no such reason to wait.
        return "breakout building, hold the trim" if score >= 80 else "trim is fine"
    # BUY / WATCH — entry timing.
    if score >= 80:
        return "good time to add"
    if score >= 50:
        return "fine to add, no rush"
    return "wait, no clean entry"


def format_stats_line(result: ConfidenceResult) -> str:
    """
    Plain-English context stats — the numeric tail of the merged 📊 alert line.
    Verdict and emoji live in the header; here every token is self-explaining,
    so a beginner can read it without knowing the confidence model.

    Examples:
      "6/13 signals agree · VCP 85 (good time to add) · past buys here: +12% after 6 months, green within ~8 days"
      "4/13 signals agree · past buys here: -4% after 6 months"
      "3/13 signals agree"
    """
    aligned = sum(1 for f in result.factors if f.score > 0)
    parts = [f"{aligned}/{result.max_score} signals agree"]

    # VCP action phrase immediately after the factor count (e.g.
    # "VCP 85 (good time to add) · pivot ₹782") — gloss baked in upstream.
    if result.vcp_summary:
        parts.insert(1, result.vcp_summary)

    # Backtest history of this level — plain English, framed as past not forecast,
    # and direction-aware so a SELL never reads "past buys here".
    if result.expectancy is not None:
        sign = "+" if result.expectancy > 0 else "−"
        pct = f"{sign}{abs(result.expectancy):.0f}%"
        if result.alert_type == "SELL":
            parts.append(f"history at this level: {pct} over the next 6 months")
        else:
            hist = f"past buys here: {pct} after 6 months"
            # Days to breakeven only meaningful (and only computed) for BUY/WATCH.
            if result.median_days is not None and result.median_days <= 60:
                d = result.median_days
                hist += f", green within ~{d} day{'s' if d != 1 else ''}"
            parts.append(hist)

    return " · ".join(parts)
