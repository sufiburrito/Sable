#!/usr/bin/env python3
"""
Fetch and cache NSE bulk deals for specific tickers.

NSE's bulk.csv archive contains only today's data. Run this daily (or on each
analysis cycle) to accumulate a historical record. If the ticker had no bulk
deals today, that is recorded so we don't re-fetch later the same day.

Usage:
    python3 fetch_bulkdeals.py BBOX
    python3 fetch_bulkdeals.py               # all active stocks

Output:
    analysis/TICKER_bulkdeals.csv   — accumulated bulk deal rows (append-only)
    analysis/data_meta.json         — tracks last-checked dates per ticker
"""
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

OUTPUT_DIR    = Path("analysis")
META_FILE     = OUTPUT_DIR / "data_meta.json"
NSE_BULK_URL  = "https://archives.nseindia.com/content/equities/bulk.csv"
NSE_BLOCK_URL = "https://archives.nseindia.com/content/equities/block.csv"


# ---------------------------------------------------------------------------
# Meta helpers (kept identical to fetch_promoter.py)
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_meta(meta: dict):
    OUTPUT_DIR.mkdir(exist_ok=True)
    META_FILE.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Main fetch function (importable)
# ---------------------------------------------------------------------------

def fetch_bulkdeals(ticker: str) -> pd.DataFrame:
    """
    Check NSE bulk.csv for today's deals for ticker. Append any new rows to
    analysis/TICKER_bulkdeals.csv and return the full accumulated DataFrame.

    If ticker was already checked today, return the existing cache without
    making a network request. Returns an empty DataFrame (not None) when there
    are no records — callers can safely check len(df).
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    cache_path = OUTPUT_DIR / f"{ticker}_bulkdeals.csv"
    meta       = _load_meta()
    today_str  = date.today().isoformat()

    already_checked = meta.get("bulkdeals", {}).get(ticker, {}).get("last_checked") == today_str

    if already_checked:
        if cache_path.exists():
            return pd.read_csv(cache_path, parse_dates=["Date"])
        return pd.DataFrame()

    # Fetch today's full bulk deal file from NSE archives
    try:
        all_deals = pd.read_csv(NSE_BULK_URL)
        all_deals.columns = all_deals.columns.str.strip()
    except Exception as e:
        print(f"  {ticker}: ERROR fetching NSE bulk.csv — {e}")
        if cache_path.exists():
            return pd.read_csv(cache_path, parse_dates=["Date"])
        return pd.DataFrame()

    # Filter for this ticker
    ticker_deals = all_deals[
        all_deals["Symbol"].str.upper().str.strip() == ticker.upper()
    ].copy()

    # Load existing cache
    if cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, parse_dates=["Date"])
        except Exception:
            existing = pd.DataFrame()
    else:
        existing = pd.DataFrame()

    # Append new rows (dedup on date + client + quantity to be safe)
    if len(ticker_deals) > 0:
        combined = pd.concat([existing, ticker_deals])
        if len(existing) > 0:
            combined = combined.drop_duplicates(
                subset=["Date", "Client Name", "Quantity Traded"]
            )
        combined.to_csv(cache_path, index=False)
        print(f"  {ticker}: {len(ticker_deals)} bulk deal(s) today → {cache_path}")
    else:
        print(f"  {ticker}: no bulk deals today")
        combined = existing

    # Fetch block deals (same daily archive, different file)
    block_deals = pd.DataFrame()
    try:
        all_blocks = pd.read_csv(NSE_BLOCK_URL)
        all_blocks.columns = all_blocks.columns.str.strip()
        block_deals = all_blocks[
            all_blocks["Symbol"].str.upper().str.strip() == ticker.upper()
        ].copy()
        if len(block_deals) > 0:
            print(f"  {ticker}: {len(block_deals)} block deal(s) today")
    except Exception as e:
        print(f"  {ticker}: block deals fetch skipped — {e}")

    # Write both to market.db (INSERT OR IGNORE — idempotent)
    try:
        import market_db
        conn = market_db.get_conn()
        _write_to_db(conn, ticker, combined, "bulk")
        _write_to_db(conn, ticker, block_deals, "block")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  {ticker}: market.db write skipped — {e}")

    # Mark as checked today regardless of whether deals were found
    meta.setdefault("bulkdeals", {}).setdefault(ticker, {})["last_checked"] = today_str
    _save_meta(meta)

    return combined if len(combined) > 0 else pd.DataFrame()


def _write_to_db(conn, ticker: str, df: pd.DataFrame, deal_type: str):
    """Write a bulk or block deals DataFrame to market.db."""
    import market_db
    for _, row in df.iterrows():
        date_raw = row.get("Date", "")
        try:
            date_str = pd.to_datetime(date_raw).date().isoformat()
        except Exception:
            date_str = str(date_raw)
        buy_sell = str(row.get("Buy/Sell", "")).strip().lower()
        market_db.upsert_bulk_deal(conn, {
            "date":        date_str,
            "ticker":      ticker,
            "client_name": str(row.get("Client Name", "") or ""),
            "trade_type":  "buy" if buy_sell.startswith("b") else "sell",
            "quantity":    int(float(str(row.get("Quantity Traded", 0) or 0))),
            "price":       float(str(row.get("Trade Price / Wght. Avg. Price", 0) or 0).replace(",", "") or 0),
            "deal_type":   deal_type,
        })


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

def print_summary(ticker: str, df: pd.DataFrame):
    if df is None or len(df) == 0:
        print(f"\n=== {ticker} — Bulk Deals: none on record ===")
        return

    display_cols = [c for c in [
        "Date", "Client Name", "Buy/Sell",
        "Quantity Traded", "Trade Price / Wght. Avg. Price"
    ] if c in df.columns]

    print(f"\n=== {ticker} — Bulk Deals ({len(df)} records) ===")
    print(df[display_cols].to_string(index=False))


if __name__ == "__main__":
    args = [a.upper() for a in sys.argv[1:] if not a.startswith("--")]

    if not args:
        from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
        from alert_bot.parser import load_all_stocks
        tickers = [s.ticker for s in load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)]
    else:
        tickers = args

    OUTPUT_DIR.mkdir(exist_ok=True)
    for ticker in tickers:
        df = fetch_bulkdeals(ticker)
        print_summary(ticker, df)
