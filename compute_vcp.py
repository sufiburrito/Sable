#!/usr/bin/env python3
"""
compute_vcp.py — Nightly VCP scorer for all active stocks.

Reads config/active_refresh_stocks.txt, loads OHLC from ohlc_cache,
runs vcp_scorer.run() per ticker, writes analysis/{TICKER}_vcp.json.

Usage: python3 compute_vcp.py [--ticker STLTECH]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
from alert_bot.ohlc_cache import load_ohlc_cached
from alert_bot.vcp_scorer import run as vcp_run

# Absolute path — anchored to repo root regardless of CWD, matching config.py convention.
ACTIVE_STOCKS_PATH = Path(__file__).parent / "config" / "active_refresh_stocks.txt"
NIFTY_YF_SYM = "^NSEI"


def _load_active_tickers(override: str | None) -> list[str]:
    """Return the list of tickers to score.

    If --ticker was passed on the CLI, return just that one ticker.
    Otherwise read config/active_refresh_stocks.txt (one ticker per line,
    lines starting with # are comments).
    """
    if override:
        return [override.upper()]
    if not ACTIVE_STOCKS_PATH.exists():
        print(f"ERROR: {ACTIVE_STOCKS_PATH} not found — check config/", file=sys.stderr)
        sys.exit(1)
    return [t.strip() for t in ACTIVE_STOCKS_PATH.read_text().splitlines()
            if t.strip() and not t.startswith("#")]


def main():
    parser = argparse.ArgumentParser(
        description="Run the VCP composite scorer for active stocks."
    )
    parser.add_argument("--ticker", help="Score a single ticker only (e.g. STLTECH)")
    args = parser.parse_args()

    tickers = _load_active_tickers(args.ticker)
    if not tickers:
        sys.exit(0)

    # Load the Nifty 50 benchmark OHLC once — every RS calculation reuses this.
    # The cache ticker is "NIFTY50" (used for the CSV filename);
    # the yfinance symbol is "^NSEI".
    print("Loading Nifty 50 benchmark…")
    bench_df = load_ohlc_cached("NIFTY50", NIFTY_YF_SYM, period="2y")

    for ticker in tickers:
        # NSE equities are suffixed with .NS in yfinance; indices use ^ prefix.
        yf_symbol = f"{ticker}.NS"
        df = load_ohlc_cached(ticker, yf_symbol, period="2y")
        if df is None or len(df) < 50:
            print(f"  {ticker}: insufficient OHLC data — skipping")
            continue
        curr_price = float(df["Close"].iloc[-1])
        if pd.isna(curr_price):
            print(f"  {ticker}: last close is NaN — skipping")
            continue
        try:
            bundle = vcp_run(ticker, df, curr_price, bench_df)
            print(f"  {ticker}: VCP {bundle['composite_score']:.0f} "
                  f"({'is_vcp' if bundle['is_vcp'] else 'no_vcp'}) "
                  f"pivot={bundle.get('pivot')}")
        except Exception as e:
            print(f"  {ticker}: ERROR — {e}")


if __name__ == "__main__":
    main()
