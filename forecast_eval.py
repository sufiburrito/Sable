#!/usr/bin/env python3
"""
Cross-validation evaluation for TradeCentral forecasting models.

Backtests Prophet and ExponentialSmoothing forecasts against held-out data
using a rolling window approach. Reports RMSE and MAE per stock per model.

Usage:
    python3 forecast_eval.py              # all stocks
    python3 forecast_eval.py BBOX SUVEN   # specific tickers

Output:
    Prints a summary table to stdout. Use this to identify which stocks
    benefit most from statistical forecasting.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
from alert_bot.ohlc_cache import load_ohlc_cached
from alert_bot.parser import load_all_stocks

# Cross-validation settings (from skill configuration template)
CV_INITIAL = 252       # ~1 year of training data (trading days)
CV_PERIOD = 63         # ~3 months between cutoff dates
CV_HORIZON = 21        # ~1 month forecast horizon


def _evaluate_exponential_smoothing(closes: pd.Series) -> dict | None:
    """Rolling CV for ExponentialSmoothing. Returns {rmse, mae, n_folds}."""
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    if len(closes) < CV_INITIAL + CV_HORIZON + CV_PERIOD:
        return None

    errors = []
    n = len(closes)
    cutoff = CV_INITIAL

    while cutoff + CV_HORIZON <= n:
        train = closes.iloc[:cutoff]
        actual = closes.iloc[cutoff:cutoff + CV_HORIZON]

        try:
            series = train.copy().asfreq("B").ffill()
            model = ExponentialSmoothing(
                series, trend="add", damped_trend=True,
                seasonal=None, initialization_method="estimated",
            )
            fit = model.fit(optimized=True)
            forecast = fit.forecast(len(actual))

            # Align by position (index may differ due to business day freq)
            pred = forecast.values[:len(actual)]
            act = actual.values[:len(pred)]
            errors.extend((pred - act).tolist())
        except Exception:
            pass

        cutoff += CV_PERIOD

    if not errors:
        return None

    errors_arr = np.array(errors)
    return {
        "rmse": float(np.sqrt(np.mean(errors_arr ** 2))),
        "mae": float(np.mean(np.abs(errors_arr))),
        "n_folds": len(errors) // CV_HORIZON,
    }


def _evaluate_prophet(closes: pd.Series) -> dict | None:
    """Rolling CV for Prophet. Returns {rmse, mae, n_folds}."""
    try:
        from prophet import Prophet
    except ImportError:
        print("  Prophet not installed, skipping.")
        return None

    if len(closes) < CV_INITIAL + CV_HORIZON + CV_PERIOD:
        return None

    errors = []
    n = len(closes)
    cutoff = CV_INITIAL

    while cutoff + CV_HORIZON <= n:
        train = closes.iloc[:cutoff]
        actual = closes.iloc[cutoff:cutoff + CV_HORIZON]

        try:
            df = pd.DataFrame({"ds": train.index, "y": train.values})
            model = Prophet(
                growth="linear",
                changepoint_prior_scale=0.1,
                seasonality_mode="multiplicative",
                daily_seasonality=False,
                weekly_seasonality=True,
                yearly_seasonality=True,
            )
            model.fit(df)

            future = model.make_future_dataframe(periods=CV_HORIZON)
            forecast = model.predict(future)
            pred = forecast["yhat"].iloc[-CV_HORIZON:].values[:len(actual)]
            act = actual.values[:len(pred)]
            errors.extend((pred - act).tolist())
        except Exception:
            pass

        cutoff += CV_PERIOD

    if not errors:
        return None

    errors_arr = np.array(errors)
    return {
        "rmse": float(np.sqrt(np.mean(errors_arr ** 2))),
        "mae": float(np.mean(np.abs(errors_arr))),
        "n_folds": len(errors) // CV_HORIZON,
    }


def main():
    all_stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)

    # Filter to requested tickers
    if len(sys.argv) > 1:
        requested = {t.upper() for t in sys.argv[1:]}
        all_stocks = [s for s in all_stocks if s.ticker in requested]

    if not all_stocks:
        print("No stocks found.")
        return

    print(f"\nForecasting cross-validation (initial={CV_INITIAL}d, "
          f"period={CV_PERIOD}d, horizon={CV_HORIZON}d)\n")
    print(f"{'Ticker':<12} {'Model':<20} {'RMSE':>10} {'MAE':>10} {'Folds':>6} {'RMSE/Price':>12}")
    print("-" * 72)

    for stock in sorted(all_stocks, key=lambda s: s.ticker):
        ticker = stock.ticker
        df = load_ohlc_cached(stock.yf_symbol, "2y")
        if df is None or len(df) < CV_INITIAL + CV_HORIZON:
            print(f"{ticker:<12} {'—':<20} {'insufficient data':>40}")
            continue

        closes = df["Close"]
        last_price = float(closes.iloc[-1])

        # ExponentialSmoothing
        es_result = _evaluate_exponential_smoothing(closes)
        if es_result:
            pct = f"{es_result['rmse']/last_price*100:.1f}%"
            print(f"{ticker:<12} {'ExpSmoothing':<20} "
                  f"₹{es_result['rmse']:>8.1f} ₹{es_result['mae']:>8.1f} "
                  f"{es_result['n_folds']:>5}  {pct:>11}")

        # Prophet
        pr_result = _evaluate_prophet(closes)
        if pr_result:
            pct = f"{pr_result['rmse']/last_price*100:.1f}%"
            print(f"{'':<12} {'Prophet':<20} "
                  f"₹{pr_result['rmse']:>8.1f} ₹{pr_result['mae']:>8.1f} "
                  f"{pr_result['n_folds']:>5}  {pct:>11}")

        if not es_result and not pr_result:
            print(f"{ticker:<12} {'—':<20} {'model fitting failed':>40}")

    print("\nRMSE/Price shows forecast error relative to current price.")
    print("Lower is better. <5% is good, <10% is usable, >15% = model struggles.")


if __name__ == "__main__":
    main()
