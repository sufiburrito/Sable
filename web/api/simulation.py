"""
Monte Carlo simulation endpoint.

Returns fan chart data (percentile bands) for a given stock,
projected forward from the most recent closing price.

When HMM regime data is available (it runs automatically), the simulation
uses regime-switching dynamics: each of the 10,000 simulated paths carries
its own regime state that evolves according to the Markov transition matrix.
This produces a fan chart that reflects the current market regime — wider in
volatile/bear periods, narrower and more optimistic in bull periods, and
asymmetric (skewed downward) during bear regimes.
"""

import yfinance as yf
from fastapi import APIRouter, HTTPException, Query

from quant_modeling.monte_carlo import run_simulation
from quant_modeling.hmm_regime import run_regime_detection

router = APIRouter(prefix="/api")


@router.get("/simulate/{ticker}")
def get_simulation(
    ticker: str,
    days: int = Query(60, ge=10, le=120, description="Trading days to simulate forward"),
):
    """
    Run Monte Carlo simulation and return fan chart percentile bands.

    The endpoint fetches 2 years of daily data from yfinance (needed for
    HMM regime detection), estimates per-regime drift and volatility,
    then runs 10,000 regime-switching GBM paths forward for `days`
    trading days.

    If regime detection fails (not enough data, HMM convergence issues),
    it falls back to single-regime GBM silently.

    Returns percentile bands (P5, P25, P50, P75, P95) that the
    frontend renders as a filled fan chart extending from the last candle.
    """
    ticker = ticker.upper()
    yf_symbol = f"{ticker}.NS"

    try:
        t = yf.Ticker(yf_symbol)
        # Fetch 2 years for HMM (also covers the 1yr needed for MC params)
        hist = t.history(period="2y", interval="1d")
    except Exception as e:
        raise HTTPException(500, f"yfinance error: {e}")

    if hist.empty or len(hist) < 30:
        raise HTTPException(404, f"Not enough price history for {ticker}")

    closes = [round(v, 2) for v in hist["Close"].tolist()]
    volumes = hist["Volume"].tolist()

    # Try to run regime detection for regime-switching simulation.
    # If it fails (insufficient data, convergence issues), we fall back
    # to single-regime GBM — the fan chart still works, just without
    # regime awareness.
    regime_data = None
    try:
        if len(closes) >= 60 and len(volumes) >= 60:
            regime_data = run_regime_detection(closes, volumes)
    except Exception:
        pass  # Silently fall back to single-regime

    try:
        result = run_simulation(
            closes,
            days_forward=days,
            n_sims=10_000,
            regime_data=regime_data,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    response = {
        "ticker": ticker,
        "days_forward": result["days"],
        "start_price": result["start_price"],
        "params": {
            "mu": round(result["params"]["mu"], 4),
            "sigma": round(result["params"]["sigma"], 4),
            "lookback_days": result["params"]["n"],
        },
        # Fan chart: each key is a percentile, value is array of prices
        # Index 0 = today (start_price for all), index N = day N
        "fan": result["fan"],
        # Whether the simulation used regime-switching dynamics
        "regime_conditional": result.get("regime_conditional", False),
    }

    # Include per-regime parameters if regime-switching was used
    # (for transparency — the frontend can show these in tooltips)
    if result.get("regime_params"):
        response["regime_params"] = result["regime_params"]

    return response
