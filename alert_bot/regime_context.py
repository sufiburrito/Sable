"""
Regime and Monte Carlo context for stock alerts.

Bridges the HMM regime detector and MC simulator (in quant_modeling/)
with the alert notification system. Produces enrichment text appended
to BUY/SELL/WATCH alerts so the user knows at a glance whether to act.

No network calls — operates on pre-computed OHLC cache files.

Two entry points:
  compute_all_regimes()      — daily scan, called once at market open
  format_regime_transition() — standalone Telegram message on regime change
"""

import logging
from pathlib import Path

import pandas as pd

from quant_modeling.hmm_regime import run_regime_detection, REGIME_INFO
from quant_modeling.monte_carlo import run_simulation

logger = logging.getLogger(__name__)

# Directory where OHLC cache CSVs live (analysis/TICKER_ohlc_cache.csv)
_ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "analysis"


# ---------------------------------------------------------------------------
# OHLC loader (mirrors floor_context._load_ohlc — same CSV format)
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
        logger.debug(f"regime_context: OHLC read error for {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# DAILY REGIME SCAN
# ---------------------------------------------------------------------------

def compute_all_regimes(stocks: list) -> dict[str, dict]:
    """
    Run HMM regime detection + Monte Carlo simulation for every stock.

    Called once daily at market open. Takes ~2-3 seconds per stock.

    Parameters:
        stocks — list of StockConfig objects (from parser.load_all_stocks)

    Returns:
        {ticker: {
            "current":       str,    # "bull" | "bear" | "sideways" | "volatile"
            "confidence":    float,  # 0.0-1.0, max probability across regimes
            "probs":         dict,   # {regime_name: probability}
            "transitions":   dict,   # Markov transition matrix
            "gbm_params":    dict,   # per-regime (mu, sigma, skewness)
            "mc_median_30d": float,  # P50 price at 30 trading days
            "mc_p25_30d":    float,  # P25 price at 30 trading days
            "mc_p75_30d":    float,  # P75 price at 30 trading days
            "last_close":    float,  # latest closing price from OHLC
        }}

    Stocks with no OHLC cache or <60 days of data are skipped silently.
    """
    results: dict[str, dict] = {}

    for stock in stocks:
        ticker = stock.ticker
        try:
            df = _load_ohlc(ticker)
            if df is None or len(df) < 60:
                logger.debug(f"regime: {ticker} — no OHLC cache or <60 days, skipping")
                continue

            closes = df["Close"].values.astype(float)
            volumes = df["Volume"].values.astype(float)
            last_close = float(closes[-1])

            # Step 1: HMM regime detection (2-year lookback, 4 states)
            regime_data = run_regime_detection(
                closes.tolist(), volumes.tolist(),
                lookback_days=504, n_states=4,
            )

            current_regime = regime_data["current"]
            confidence = regime_data["probs"].get(current_regime, 0.0)

            # Step 2: Monte Carlo simulation (30 trading days forward)
            # Uses regime-switching dynamics from the HMM's transition matrix
            mc_result = run_simulation(
                closes.tolist(),
                days_forward=30,
                n_sims=10_000,
                lookback_days=252,
                regime_data=regime_data,
            )

            # Extract P25, P50, P75 at the final day (index 30)
            fan = mc_result["fan"]
            mc_median = fan[50][-1]   # P50 at day 30
            mc_p25 = fan[25][-1]      # P25 at day 30
            mc_p75 = fan[75][-1]      # P75 at day 30

            results[ticker] = {
                "current": current_regime,
                "confidence": round(confidence, 4),
                "probs": regime_data["probs"],
                "transitions": regime_data["transitions"],
                "gbm_params": regime_data.get("gbm_params", {}),
                "mc_median_30d": round(mc_median, 2),
                "mc_p25_30d": round(mc_p25, 2),
                "mc_p75_30d": round(mc_p75, 2),
                "last_close": round(last_close, 2),
            }

            logger.info(
                f"regime: {ticker} → {current_regime} "
                f"({confidence:.0%}), MC ₹{mc_median:.0f} in 30d"
            )

        except Exception as e:
            logger.warning(f"regime: {ticker} — failed: {e}")
            continue

    logger.info(f"regime: scanned {len(results)}/{len(stocks)} stocks")
    return results


# ---------------------------------------------------------------------------
# REGIME TRANSITION ALERT
# ---------------------------------------------------------------------------

def format_regime_transition(
    ticker: str,
    prev_regime: str,
    new_regime: str,
    confidence: float,
    mc_median_30d: float,
    curr_price: float,
) -> str:
    """
    Format a standalone Telegram message for a regime transition.

    Example:
        📊  REGIME SHIFT  CGPOWER
        ⚪ Sideways → 🟢 Bull (83%)
        MC median ₹890 in 30d (+6% from ₹838)
        → BUY levels below current price are now higher conviction

    Returns HTML-formatted string for Telegram (parse_mode=HTML).
    """
    # Get emojis for old and new regime
    prev_emoji = REGIME_INFO.get(prev_regime, {}).get("emoji", "⚪")
    new_emoji = REGIME_INFO.get(new_regime, {}).get("emoji", "⚪")

    conf_pct = f"{confidence:.0%}"

    # MC delta
    if curr_price > 0:
        delta_pct = (mc_median_30d - curr_price) / curr_price * 100
        sign = "+" if delta_pct >= 0 else ""
        mc_line = f"MC median ₹{mc_median_30d:,.0f} in 30d ({sign}{delta_pct:.0f}% from ₹{curr_price:,.0f})"
    else:
        mc_line = f"MC median ₹{mc_median_30d:,.0f} in 30d"

    # Action guidance based on the NEW regime
    if new_regime == "bull":
        action = "→ BUY levels below current price are now higher conviction"
    elif new_regime == "bear":
        action = "→ Hold core, avoid adding — wait for regime turn"
    elif new_regime == "sideways":
        action = "→ Trade the range — buy support, trim resistance"
    else:  # volatile
        action = "→ Reduce exposure — wait for clarity"

    prev_label = prev_regime.capitalize()
    new_label = new_regime.capitalize()

    return (
        f"<b>📊  REGIME SHIFT  {ticker}</b>\n"
        f"{prev_emoji} {prev_label} → {new_emoji} {new_label} ({conf_pct})\n"
        f"{mc_line}\n"
        f"{action}"
    )
