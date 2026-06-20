#!/usr/bin/env python3
"""
Fetch and cache quarterly promoter shareholding from Screener.in.

Re-fetches only when a new quarter's data is expected (~85 days after quarter-end),
or when --force is passed.

Usage:
    python3 fetch_promoter.py BBOX
    python3 fetch_promoter.py BBOX SUVEN CGPOWER
    python3 fetch_promoter.py --force BBOX      # bypass freshness check

Output:
    analysis/TICKER_promoter.csv   — full quarterly history (overwritten on each fetch)
    analysis/data_meta.json        — tracks last-fetch dates to avoid redundant calls
"""
import json
import sys
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

OUTPUT_DIR = Path("analysis")
META_FILE  = OUTPUT_DIR / "data_meta.json"

SCREENER_URL = "https://www.screener.in/company/{ticker}/consolidated/"
HEADERS      = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Map Screener.in month abbreviation → MM-DD of quarter-end
_QUARTER_END = {"Mar": "03-31", "Jun": "06-30", "Sep": "09-30", "Dec": "12-31"}


# ---------------------------------------------------------------------------
# Meta helpers (shared with fetch_bulkdeals.py — keep identical)
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
# Freshness logic
# ---------------------------------------------------------------------------

def _last_completed_quarter_end() -> date:
    """Return the end date of the most recently completed calendar quarter."""
    today = date.today()
    m = today.month
    if m <= 3:
        return date(today.year - 1, 12, 31)
    elif m <= 6:
        return date(today.year, 3, 31)
    elif m <= 9:
        return date(today.year, 6, 30)
    else:
        return date(today.year, 9, 30)


def _new_data_expected_after() -> date:
    """Screener.in typically reflects new quarter ~85 days after quarter-end."""
    return _last_completed_quarter_end() + timedelta(days=85)


def _cache_is_fresh(ticker: str, meta: dict) -> bool:
    last_fetched_str = meta.get("promoter", {}).get(ticker, {}).get("last_fetched")
    if not last_fetched_str:
        return False
    last_fetched  = datetime.strptime(last_fetched_str, "%Y-%m-%d").date()
    today         = date.today()
    next_expected = _new_data_expected_after()
    if today < next_expected:
        # No new quarter data expected yet — any prior fetch is sufficient
        return True
    # New quarter data should be out — fresh only if we fetched after it became available
    return last_fetched >= next_expected


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _quarter_col_to_date(col: str) -> str | None:
    """'Mar 2023' → '2023-03-31', or None if unrecognised."""
    parts = str(col).strip().split()
    if len(parts) != 2 or parts[0] not in _QUARTER_END:
        return None
    return f"{parts[1]}-{_QUARTER_END[parts[0]]}"


def _parse_shareholding(tables: list) -> pd.DataFrame | None:
    """Find the shareholding table and return a tidy DataFrame."""
    sh = None
    for t in tables:
        if t.iloc[:, 0].astype(str).str.contains("Promoter", na=False).any():
            sh = t
            break
    if sh is None:
        return None

    row_keys = {
        "Promoters": "promoter_pct",
        "FIIs":      "fii_pct",
        "DIIs":      "dii_pct",
        "Public":    "public_pct",
        "No. of Shareholders": "shareholders",
    }

    parsed_rows: dict[str, pd.Series] = {}
    for _, row in sh.iterrows():
        label = str(row.iloc[0]).replace(" +", "").strip()
        for key, col_name in row_keys.items():
            if key in label:
                parsed_rows[col_name] = row
                break

    if "promoter_pct" not in parsed_rows:
        return None

    quarter_cols = sh.columns[1:]  # skip the label column
    records = []
    for col in quarter_cols:
        date_str = _quarter_col_to_date(str(col))
        if not date_str:
            continue
        record: dict = {"date": date_str}
        for col_name, row in parsed_rows.items():
            raw = str(row[col]).replace("%", "").replace(",", "").strip()
            try:
                record[col_name] = float(raw)
            except ValueError:
                record[col_name] = None
        records.append(record)

    if not records:
        return None

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").set_index("date")


# ---------------------------------------------------------------------------
# Main fetch function (importable)
# ---------------------------------------------------------------------------

def fetch_promoter(ticker: str, force: bool = False) -> pd.DataFrame | None:
    """
    Return quarterly promoter shareholding for ticker as a DataFrame.

    Columns: promoter_pct, fii_pct, dii_pct, public_pct, shareholders
    Index:   quarter-end dates (datetime)

    Uses cache unless new quarterly data is expected or force=True.
    Falls back to stale cache if Screener.in is unreachable.
    Returns None if no data at all.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    cache_path = OUTPUT_DIR / f"{ticker}_promoter.csv"
    meta       = _load_meta()

    if not force and _cache_is_fresh(ticker, meta) and cache_path.exists():
        print(f"  {ticker}: promoter cache current (next data expected ~{_new_data_expected_after()})")
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df

    print(f"  {ticker}: fetching promoter data from Screener.in…")
    try:
        r = requests.get(SCREENER_URL.format(ticker=ticker), headers=HEADERS, timeout=15)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
    except Exception as e:
        print(f"  {ticker}: Screener.in fetch failed — {e}")
        if cache_path.exists():
            print(f"  {ticker}: returning stale cache")
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            return df
        return None

    df = _parse_shareholding(tables)
    if df is None:
        print(f"  {ticker}: could not parse shareholding table")
        if cache_path.exists():
            cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            cached.index = pd.to_datetime(cached.index).tz_localize(None)
            return cached
        return None

    df.to_csv(cache_path)

    meta.setdefault("promoter", {})[ticker] = {"last_fetched": date.today().isoformat()}
    _save_meta(meta)

    # Write to market.db (idempotent — INSERT OR REPLACE)
    try:
        import market_db
        conn = market_db.get_conn()
        for qdate, row in df.iterrows():
            market_db.upsert_promoter_holding(conn, {
                "quarter_date": qdate.date().isoformat(),
                "ticker":       ticker,
                "promoter_pct": row.get("promoter_pct"),
                "fii_pct":      row.get("fii_pct"),
                "dii_pct":      row.get("dii_pct"),
                "public_pct":   row.get("public_pct"),
                "shareholders": row.get("shareholders"),
            })
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  {ticker}: market.db write skipped — {e}")

    print(f"  {ticker}: {len(df)} quarters saved → {cache_path}")
    return df


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

def print_summary(ticker: str, df: pd.DataFrame):
    print(f"\n=== {ticker} — Promoter Shareholding ===")
    display_cols = [c for c in ["promoter_pct", "fii_pct", "dii_pct"] if c in df.columns]
    print(df[display_cols].to_string())

    if "promoter_pct" not in df.columns or len(df) < 2:
        return

    first = df["promoter_pct"].iloc[0]
    last  = df["promoter_pct"].iloc[-1]
    delta = last - first
    trend = "↑ INCREASING" if delta > 0.3 else "↓ DECREASING" if delta < -0.3 else "≈ STABLE"
    print(f"\nPromoter trend: {first:.2f}% → {last:.2f}% ({delta:+.2f}pp)  {trend}")

    # Flag quarters with notable moves (≥0.3pp)
    diffs = df["promoter_pct"].diff().dropna()
    notable = diffs[diffs.abs() >= 0.3]
    if not notable.empty:
        print("Notable quarterly changes:")
        for dt, chg in notable.items():
            label = "↑ BOUGHT" if chg > 0 else "↓ SOLD"
            print(f"  {dt.strftime('%b %Y')}: {chg:+.2f}pp  {label}")


if __name__ == "__main__":
    args  = sys.argv[1:]
    force = "--force" in args
    args  = [a for a in args if a != "--force"]

    if not args:
        from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
        from alert_bot.parser import load_all_stocks
        tickers = [s.ticker for s in load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)]
    else:
        tickers = [a.upper() for a in args]

    OUTPUT_DIR.mkdir(exist_ok=True)
    for ticker in tickers:
        df = fetch_promoter(ticker, force=force)
        if df is not None:
            print_summary(ticker, df)
