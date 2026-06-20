"""
Unified time series forecaster for TradeCentral.

Wraps statsmodels ExponentialSmoothing (Phase 1) and ARIMA (Phase 2),
with Prophet (Phase 3) added later. Each function returns plain numbers
or dicts — no model internals leak to the caller.

All inputs come from local OHLC cache CSVs — no network calls.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Minimum data points required for a meaningful forecast
_MIN_OBSERVATIONS = 30


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TrendForecast:
    """Short-term trend forecast from ExponentialSmoothing."""
    predicted: list[float]     # forecasted Close prices for each day
    lower: list[float]         # lower bound (80% CI)
    upper: list[float]         # upper bound (80% CI)
    trend_direction: str       # "up", "down", or "flat"
    trend_strength: float      # magnitude of daily trend component (₹/day)
    confidence: float          # 0-1 — how well the model fits recent data


# ---------------------------------------------------------------------------
# Exponential Smoothing — short-term trend for floor/ceiling hints
# ---------------------------------------------------------------------------

def trend_forecast(
    closes: pd.Series,
    horizon: int = 10,
    confidence: float = 0.80,
) -> TrendForecast | None:
    """
    Fit damped Exponential Smoothing on daily Close prices and forecast
    `horizon` days forward.

    Args:
        closes: Daily Close prices (DatetimeIndex), at least _MIN_OBSERVATIONS long.
        horizon: Number of days to forecast.
        confidence: Confidence interval width (0-1).

    Returns:
        TrendForecast with predicted values, CI bounds, and trend direction.
        None if insufficient data or model fails to converge.
    """
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    if len(closes) < _MIN_OBSERVATIONS:
        return None

    # Use last 90 days max — older data adds noise for short-term trend
    series = closes.iloc[-90:].copy()
    series = series.asfreq("B")  # business-day frequency, forward-fill gaps
    series = series.ffill()

    try:
        # Damped additive trend, no seasonal component
        # Damping prevents runaway extrapolation — critical for alerting
        model = ExponentialSmoothing(
            series,
            trend="add",
            damped_trend=True,
            seasonal=None,
            initialization_method="estimated",
        )
        fit = model.fit(optimized=True)

        # Point forecast
        forecast = fit.forecast(horizon)
        predicted = forecast.values.tolist()

        # Confidence interval via simulation (ExponentialSmoothing doesn't have
        # built-in PI, so we estimate from in-sample residual std)
        residuals = fit.resid.dropna()
        resid_std = float(residuals.std())

        # Scale CI by sqrt(horizon step) — uncertainty grows with time
        from scipy.stats import norm
        z = norm.ppf(0.5 + confidence / 2)  # e.g. 1.28 for 80% CI
        lower = []
        upper = []
        for i in range(horizon):
            spread = z * resid_std * np.sqrt(i + 1)
            lower.append(predicted[i] - spread)
            upper.append(predicted[i] + spread)

        # Trend direction: compare first and last predicted values
        delta = predicted[-1] - predicted[0]
        last_price = float(series.iloc[-1])
        # "flat" if total move is < 0.5% of price
        if abs(delta) < last_price * 0.005:
            direction = "flat"
        elif delta > 0:
            direction = "up"
        else:
            direction = "down"

        # Trend strength: average daily change from the fitted trend component
        trend_strength = delta / horizon

        # Confidence: 1 - (normalized RMSE) — how well model fits recent data
        rmse = np.sqrt(np.mean(residuals[-20:] ** 2)) if len(residuals) >= 20 else resid_std
        norm_rmse = rmse / last_price
        model_confidence = max(0.0, min(1.0, 1.0 - norm_rmse * 5))

        return TrendForecast(
            predicted=predicted,
            lower=lower,
            upper=upper,
            trend_direction=direction,
            trend_strength=float(trend_strength),
            confidence=model_confidence,
        )

    except Exception as e:
        logger.debug(f"forecaster: ExponentialSmoothing failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Prophet — medium/long-term forecast (Phase 3, placeholder)
# ---------------------------------------------------------------------------

def prophet_forecast(
    closes: pd.Series,
    horizons: list[int] | None = None,
    confidence: float = 0.80,
) -> dict | None:
    """
    Prophet forecast for 30/60/90 day horizons.

    Returns dict with keys for each horizon:
        {30: {"lower": X, "predicted": Y, "upper": Z, "trend": "up"}, ...}

    Returns None if prophet is not installed or data is insufficient.
    """
    if horizons is None:
        horizons = [30, 60, 90]

    try:
        from prophet import Prophet
    except ImportError:
        logger.debug("forecaster: prophet not installed, skipping long-term forecast")
        return None

    if len(closes) < 120:  # need at least ~6 months for meaningful Prophet fit
        return None

    # Prophet expects a DataFrame with columns 'ds' and 'y'
    df = pd.DataFrame({
        "ds": closes.index,
        "y": closes.values,
    })

    try:
        # Suppress Prophet's verbose logging
        import logging as _logging
        _logging.getLogger("prophet").setLevel(_logging.WARNING)
        _logging.getLogger("cmdstanpy").setLevel(_logging.WARNING)

        model = Prophet(
            growth="linear",
            changepoint_prior_scale=0.05,      # conservative to avoid runaway extrapolation
            seasonality_mode="multiplicative",  # % moves scale with price
            interval_width=confidence,
            daily_seasonality=False,
            weekly_seasonality=True,
            yearly_seasonality=True,
        )
        model.fit(df)

        max_horizon = max(horizons)
        future = model.make_future_dataframe(periods=max_horizon)
        forecast = model.predict(future)

        current = float(closes.iloc[-1])
        # Sanity bounds: use 52-week range expanded by 10% as hard limits
        # This prevents runaway extrapolation while staying market-realistic
        recent = closes.iloc[-252:] if len(closes) >= 252 else closes
        hist_low = float(recent.min())
        hist_high = float(recent.max())
        floor_bound = hist_low * 0.90   # 10% below 52-week low
        ceil_bound = hist_high * 1.10   # 10% above 52-week high

        # Extract values at each horizon
        result = {}
        last_idx = len(df) - 1
        for h in horizons:
            row = forecast.iloc[last_idx + h]
            raw_pred = float(row["yhat"])
            raw_lower = float(row["yhat_lower"])
            raw_upper = float(row["yhat_upper"])

            # Clamp to sanity bounds
            predicted = max(floor_bound, min(ceil_bound, raw_pred))
            lower = max(floor_bound, raw_lower)
            upper = min(ceil_bound, raw_upper)

            # Track if clamping was needed (= low confidence at this horizon)
            was_clamped = (raw_pred != predicted or raw_lower != lower or raw_upper != upper)

            # Ensure lower <= predicted <= upper after clamping
            lower = min(lower, predicted)
            upper = max(upper, predicted)

            delta_pct = (predicted - current) / current * 100
            if abs(delta_pct) < 2:
                trend = "sideways"
            elif delta_pct > 0:
                trend = "up"
            else:
                trend = "down"

            result[h] = {
                "lower": round(lower, 1),
                "predicted": round(predicted, 1),
                "upper": round(upper, 1),
                "trend": trend,
                "clamped": was_clamped,
            }

        # Drop any clamped horizon — unreliable predictions
        result = {h: v for h, v in result.items() if not v.get("clamped")}

        # Clean up the clamped flag from remaining results
        for v in result.values():
            v.pop("clamped", None)

        return result if result else None

    except Exception as e:
        logger.debug(f"forecaster: Prophet failed: {e}")
        return None
