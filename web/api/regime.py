"""
HMM regime detection endpoint.

Returns the current regime, historical regime sequence (for chart
background coloring), probabilities, and transition matrix.
"""

import yfinance as yf
from fastapi import APIRouter, HTTPException

from quant_modeling.hmm_regime import run_regime_detection

router = APIRouter(prefix="/api")


@router.get("/regime/{ticker}")
def get_regime(ticker: str):
    """
    Run HMM regime detection and return results for chart and UI.

    Fetches 2 years of daily data, trains a 4-state Gaussian HMM,
    and returns the regime sequence aligned to the price history.
    """
    ticker = ticker.upper()
    yf_symbol = f"{ticker}.NS"

    try:
        t = yf.Ticker(yf_symbol)
        hist = t.history(period="2y", interval="1d")
    except Exception as e:
        raise HTTPException(500, f"yfinance error: {e}")

    if hist.empty or len(hist) < 60:
        raise HTTPException(404, f"Not enough price history for {ticker}")

    closes = hist["Close"].tolist()
    volumes = hist["Volume"].tolist()
    dates = [idx.strftime("%Y-%m-%d") for idx in hist.index]

    try:
        result = run_regime_detection(closes, volumes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Align regime labels to dates.
    # The HMM uses a rolling window for feature computation, so the
    # first `offset` days don't have regime labels. We pad those with
    # null so the arrays match the full date range.
    offset = result["offset"]
    padded_regimes = [None] * offset + result["regimes"]

    return {
        "ticker": ticker,
        "current": result["current"],
        "probs": result["probs"],
        "regime_info": result["regime_info"],
        "params": result["params"],
        "transitions": result["transitions"],
        # Per-regime annualized GBM parameters (mu, sigma, skewness)
        # with shrinkage applied to sparse regimes. These are what the
        # regime-switching Monte Carlo simulation uses for each regime.
        "gbm_params": result.get("gbm_params", {}),
        # Per-day regime data aligned to the full 2-year date range.
        # Frontend will match these dates to chart x-axis labels.
        "dates": dates,
        "regimes": padded_regimes,
    }
