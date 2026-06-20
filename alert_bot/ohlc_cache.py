"""
Shared OHLC daily-bar cache for export_stock.py and retrospective_analysis.py.

Cache location: analysis/TICKER_ohlc_cache.csv
Format: standard yfinance daily OHLCV (Date index, Open/High/Low/Close/Volume columns)

Both scripts read and write this cache.  Only the missing tail is fetched from
yfinance on each run, so the expensive historical download happens at most once
per ticker.
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

ANALYSIS_DIR = Path("analysis")

_PERIOD_DAYS: dict[str, int] = {
    "1y": 365, "2y": 730, "3y": 1095, "5y": 1825, "max": 10000,
}


def read_ohlc_cache(ticker: str, analysis_dir: Path | None = None) -> pd.DataFrame | None:
    """
    Read a ticker's cached OHLC CSV into a Date-indexed DataFrame, with NO
    network call. Returns None if the cache is missing, empty, or unreadable.

    This is the no-fetch sibling of load_ohlc_cached(). The alert hot path
    (floor_context, regime_context, confidence) is forbidden from triggering a
    yfinance fetch (CLAUDE.md: "No network calls in the alert hot path"), so it
    reads the CSV directly. Before this primitive existed, three modules each
    hand-rolled the read — two with the canonical Date-as-index shape and one
    (confidence) with Date-as-column — which is the divergence bean ne2m closes.

    The frame is Date-indexed (tz-naive) and sorted ascending. *analysis_dir*
    lets each caller preserve its own directory resolution (some anchor to the
    repo root, some are CWD-relative); it defaults to the module ANALYSIS_DIR.
    """
    base = analysis_dir if analysis_dir is not None else ANALYSIS_DIR
    cache_path = base / f"{ticker}_ohlc_cache.csv"
    if not cache_path.exists():
        return None
    try:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if df.empty:
            return None
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df.sort_index()
    except Exception:
        return None


def load_ohlc_cached(ticker: str, yf_symbol: str, period: str = "2y") -> pd.DataFrame:
    """
    Return a daily OHLCV DataFrame for yf_symbol, backed by a local CSV cache.

    Behaviour:
    - First call for a ticker: downloads `period` of history from yfinance,
      saves to analysis/TICKER_ohlc_cache.csv, returns the full DataFrame.
    - Subsequent calls: reads the cache, fetches only bars newer than the
      last cached date from yfinance, appends them, and saves the updated cache.
    - If the cache doesn't reach back far enough for the requested period,
      does a full re-fetch from yfinance.

    The cache always grows (new bars are appended, old bars are never trimmed),
    so a 2y retro run followed by a 1y export run will reuse the full 2y cache.
    """
    ANALYSIS_DIR.mkdir(exist_ok=True)
    cache_path = ANALYSIS_DIR / f"{ticker}_ohlc_cache.csv"

    required_days  = _PERIOD_DAYS.get(period, 730)
    required_start = pd.Timestamp.now().normalize() - pd.Timedelta(days=required_days + 30)

    if cache_path.exists():
        try:
            cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            cached.index = pd.to_datetime(cached.index).tz_localize(None)

            if len(cached) > 0 and cached.index[0] <= required_start + pd.Timedelta(days=30):
                cache_end = cached.index[-1]
                today     = pd.Timestamp.now().normalize()

                if cache_end.date() >= today.date():
                    return cached  # already up to date

                # Fetch only the missing tail
                start_str = (cache_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                new_df = yf.Ticker(yf_symbol).history(start=start_str, interval="1d")

                if len(new_df) > 0:
                    new_df.index = (
                        new_df.index.tz_localize(None)
                        if new_df.index.tzinfo
                        else new_df.index
                    )
                    new_rows = new_df[~new_df.index.isin(cached.index)]
                    if len(new_rows) > 0:
                        combined = pd.concat([cached, new_rows])
                        combined.to_csv(cache_path)
                        print(f"    Cache updated: +{len(new_rows)} new bar(s) → {cache_path.name}")
                        return combined
                return cached
        except Exception as e:
            print(f"    Cache read error ({e}), re-fetching from yfinance…")

    # Full fetch (first run or cache too short / corrupt)
    print(f"    Fetching {period} history from yfinance…")
    df = yf.Ticker(yf_symbol).history(period=period, interval="1d")
    if df.empty:
        return df
    df.index = df.index.tz_localize(None) if df.index.tzinfo else df.index

    # Merge with any existing cache — never discard previously downloaded bars
    if cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            existing.index = pd.to_datetime(existing.index).tz_localize(None)
            df = pd.concat([existing, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        except Exception:
            pass  # corrupt cache — just use the fresh fetch

    df.to_csv(cache_path)
    print(f"    Cache saved: {len(df)} bars → {cache_path}")
    return df
