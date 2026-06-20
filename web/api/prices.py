"""
Price data endpoint.
Fetches OHLCV from yfinance and computes moving averages.
"""
import numpy as np
import yfinance as yf
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api")

# Period → interval mapping (finer granularity for short timeframes)
# yfinance limits: 15m → max 60 days, 1h → max 730 days, 1d → unlimited
_INTERVALS = {
    "1d": "5m",    # ~78 candles
    "5d": "15m",   # ~130 candles
    "1mo": "1h",   # ~150 candles
    "3mo": "1d",   # ~63 candles
    "6mo": "1d",   # ~126 candles
    "1y": "1d",    # ~252 candles
}

# Periods that use intraday intervals (need datetime, not just date)
_INTRADAY_PERIODS = {"1d", "5d", "1mo"}


def _compute_ma(closes: list[float], window: int) -> list[float | None]:
    """Simple moving average. Returns None for indices < window-1."""
    result = []
    for i in range(len(closes)):
        if i < window - 1:
            result.append(None)
        else:
            result.append(round(np.mean(closes[i - window + 1 : i + 1]), 2))
    return result


@router.get("/prices/{ticker}")
def get_prices(
    ticker: str,
    period: str = Query("1mo", pattern="^(1d|5d|1mo|3mo|6mo|1y)$"),
):
    """Return OHLCV data + moving averages for charting."""
    ticker = ticker.upper()
    yf_symbol = f"{ticker}.NS"
    interval = _INTERVALS.get(period, "1d")

    try:
        t = yf.Ticker(yf_symbol)
        hist = t.history(period=period, interval=interval)
    except Exception as e:
        raise HTTPException(500, f"yfinance error: {e}")

    if hist.empty:
        raise HTTPException(404, f"No price data for {ticker}")

    # Convert to display strings (include time for intraday intervals)
    dates = []
    for idx in hist.index:
        if period in _INTRADAY_PERIODS:
            dates.append(idx.strftime("%Y-%m-%d %H:%M"))
        else:
            dates.append(idx.strftime("%Y-%m-%d"))

    opens = [round(v, 2) for v in hist["Open"].tolist()]
    highs = [round(v, 2) for v in hist["High"].tolist()]
    lows = [round(v, 2) for v in hist["Low"].tolist()]
    closes = [round(v, 2) for v in hist["Close"].tolist()]
    volumes = [int(v) for v in hist["Volume"].tolist()]

    # Compute moving averages (only meaningful for daily candles)
    is_daily = _INTERVALS.get(period) == "1d"
    ma20 = _compute_ma(closes, 20) if is_daily else None
    ma50 = _compute_ma(closes, 50) if is_daily else None
    ma200 = _compute_ma(closes, 200) if is_daily else None

    return {
        "ticker": ticker,
        "period": period,
        "interval": interval,
        "dates": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
    }
