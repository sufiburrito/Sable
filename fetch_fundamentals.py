#!/usr/bin/env python3
"""
Fetch and cache quarterly fundamentals from Screener.in.

Re-fetches only when data is older than 60 days (one NSE results cycle),
or when --force is passed. On a cache hit prints "CACHED" and makes no
network call.

Usage:
    python3 fetch_fundamentals.py STLTECH
    python3 fetch_fundamentals.py STLTECH BBOX SUVEN
    python3 fetch_fundamentals.py --force STLTECH   # bypass TTL

Output:
    data/market.db → fundamentals table (quarterly rows per ticker)
    Pledge data is updated in-place on the most recent quarter's row.
"""
import sys
from io import StringIO
from pathlib import Path

import requests
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from market_db import get_conn, upsert_fundamentals, is_fundamentals_fresh

SCREENER_URL = "https://www.screener.in/company/{ticker}/consolidated/"
HEADERS      = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Screener.in quarter-end month → MM-DD (same convention as fetch_promoter.py)
_MONTH_TO_QE = {"Mar": "03-31", "Jun": "06-30", "Sep": "09-30", "Dec": "12-31"}


def _col_to_period(col: str) -> str | None:
    """'Mar 2026' → '2026-03-31', consistent with promoter_holdings quarter_date."""
    parts = str(col).strip().split()
    if len(parts) != 2 or parts[0] not in _MONTH_TO_QE:
        return None
    return f"{parts[1]}-{_MONTH_TO_QE[parts[0]]}"


def _safe_float(val) -> float | None:
    try:
        s = str(val).replace(",", "").replace("%", "").strip()
        return None if s in ("", "-", "—", "N/A", "nan", "None") else float(s)
    except (ValueError, TypeError):
        return None


def _find_row(table: pd.DataFrame, pattern: str) -> "pd.Series | None":
    mask = table.iloc[:, 0].astype(str).str.contains(pattern, na=False, case=False, regex=True)
    return table[mask].iloc[0] if mask.any() else None


def fetch_fundamentals(ticker: str, force: bool = False) -> bool:
    """
    Fetch fundamentals for ticker. Returns True if a network call was made.
    On cache hit (fresh within 60 days): prints CACHED, returns False.
    """
    conn = get_conn()

    if not force and is_fundamentals_fresh(conn, ticker, ttl_days=60):
        print(f"  {ticker}: fundamentals CACHED (no fetch needed)")
        conn.close()
        return False

    print(f"  {ticker}: fetching fundamentals from Screener.in...")
    try:
        r = requests.get(SCREENER_URL.format(ticker=ticker), headers=HEADERS, timeout=20)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
    except Exception as e:
        print(f"  {ticker}: Screener.in fetch failed — {e}")
        conn.close()
        return False

    rows_written = 0

    # --- Quarterly P&L (Revenue + Net Profit) ---
    # Row labels on Screener.in have a \xa0+ suffix (e.g. "Sales\xa0+"),
    # so we use partial matching (no anchors) throughout this file.
    for t in tables:
        first_col = t.iloc[:, 0].astype(str)
        if not (first_col.str.contains("Sales", na=False, case=False).any() or
                first_col.str.contains("Revenue", na=False, case=False).any()):
            continue
        # Skip annual report tables — their date columns are all "Mar YYYY".
        # Quarterly results tables have mixed months (Mar, Jun, Sep, Dec).
        date_cols = [c for c in t.columns[1:] if _col_to_period(str(c))]
        months = {str(c).strip().split()[0] for c in date_cols
                  if len(str(c).strip().split()) == 2}
        if months and months == {"Mar"}:
            continue
        rev_row = _find_row(t, r"Sales|Revenue")
        pat_row = _find_row(t, r"Net Profit")
        for col in t.columns[1:]:
            period = _col_to_period(str(col))
            if not period:
                continue
            upsert_fundamentals(conn, {
                "ticker":        ticker,
                "period":        period,
                "period_type":   "quarterly",
                "revenue_cr":    _safe_float(rev_row[col]) if rev_row is not None else None,
                "net_profit_cr": _safe_float(pat_row[col]) if pat_row is not None else None,
            })
            rows_written += 1
        break

    # --- Annual Ratios (ROCE, ROE, Debt/Equity, P/E, P/B) ---
    # Screener.in shows these as yearly (Mar YYYY) — store as period_type="annual".
    for t in tables:
        if not t.iloc[:, 0].astype(str).str.contains("ROCE", na=False).any():
            continue
        roce_row = _find_row(t, "ROCE")
        roe_row  = _find_row(t, r"ROE|Return on Equity")
        de_row   = _find_row(t, r"Debt.*Equity|D/E")
        pe_row   = _find_row(t, r"Price.*Earn")
        pb_row   = _find_row(t, r"Price.*Book")
        for col in t.columns[1:]:
            period = _col_to_period(str(col))
            if not period:
                continue
            upsert_fundamentals(conn, {
                "ticker":      ticker,
                "period":      period,
                "period_type": "annual",
                "roce_pct":    _safe_float(roce_row[col]) if roce_row is not None else None,
                "roe_pct":     _safe_float(roe_row[col])  if roe_row  is not None else None,
                "debt_equity": _safe_float(de_row[col])   if de_row   is not None else None,
                "pe_ratio":    _safe_float(pe_row[col])   if pe_row   is not None else None,
                "pb_ratio":    _safe_float(pb_row[col])   if pb_row   is not None else None,
            })
        break

    # --- Promoter pledge % — fetch via Screener.in login (requires Playwright) ---
    # Screener.in does not expose pledge data in static HTML; it requires a logged-in
    # session where the shareholding table is JS-rendered. We use browser_utils to log
    # in headlessly and extract the latest pledged % value.
    pledge_val = _fetch_pledge_playwright(ticker)
    if pledge_val is not None:
        # Write to the most recent quarterly row for this ticker
        conn.execute("""
            UPDATE fundamentals SET promoter_pledge_pct = ?
            WHERE ticker = ? AND period_type = 'quarterly'
              AND period = (SELECT MAX(period) FROM fundamentals
                            WHERE ticker = ? AND period_type = 'quarterly')
        """, (pledge_val, ticker, ticker))
        print(f"  {ticker}: pledge {pledge_val}% written")

    conn.commit()
    print(f"  {ticker}: {rows_written} quarterly rows written to fundamentals table")
    conn.close()
    return True


def _fetch_pledge_playwright(ticker: str) -> float | None:
    """
    Use Screener.in login to get the most recent promoter pledged-shares %.
    Returns None if credentials are not configured or Playwright is unavailable.
    A return value of 0.0 means the company genuinely has zero pledge.
    Called only from fetch_fundamentals() — the 60-day TTL prevents redundant sessions.
    """
    import os
    email    = os.environ.get("SCREENER_EMAIL")
    password = os.environ.get("SCREENER_PASSWORD")
    if not email or not password:
        return None   # credentials not configured — silent skip
    try:
        from browser_utils import scrape_screener_pledge
        return scrape_screener_pledge(ticker, email, password, headless=True)
    except ImportError:
        return None   # playwright not installed — silent skip


if __name__ == "__main__":
    args  = sys.argv[1:]
    force = "--force" in args
    args  = [a for a in args if not a.startswith("--")]

    if not args:
        from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
        from alert_bot.parser import load_all_stocks
        tickers = [s.ticker for s in load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)]
    else:
        tickers = [a.upper() for a in args]

    for ticker in tickers:
        fetch_fundamentals(ticker, force=force)
