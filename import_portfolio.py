"""
import_portfolio.py — Groww brokerage export → local SQLite portfolio database.

PII stripping: rows 0–1 of every Groww sheet contain Name and Unique Client Code.
This script skips those rows entirely. They are never read into memory or written anywhere.
The resulting database (data/portfolio.db) contains only financial data: tickers, ISINs,
quantities, prices, dates.

Usage:
    python3 import_portfolio.py                          # auto-detect xlsx in stock portfolio/
    python3 import_portfolio.py --force                  # reimport even if xlsx files unchanged
    python3 import_portfolio.py --holdings path/to.xlsx
    python3 import_portfolio.py --orders   path/to.xlsx
    python3 import_portfolio.py --holdings h.xlsx --orders o.xlsx
"""

import argparse
import glob
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path("data/portfolio.db")
EXPORT_DIR = Path("stock portfolio")

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_name          TEXT    NOT NULL,
    symbol              TEXT,
    isin                TEXT    NOT NULL,
    trade_type          TEXT    NOT NULL,
    quantity            INTEGER NOT NULL,
    total_value         REAL    NOT NULL,
    -- NULL for zero-value corporate actions (rights entitlements, delisting squeeze-outs)
    price_per_share     REAL,
    exchange            TEXT,
    -- dedup key: re-importing the same file is safe
    exchange_order_id   TEXT    UNIQUE,
    executed_at         DATETIME NOT NULL,
    -- 1 for zero-value entries such as rights entitlements and delisting compensation
    is_corporate_action INTEGER NOT NULL DEFAULT 0,
    imported_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS holdings_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   DATE    NOT NULL,
    stock_name      TEXT    NOT NULL,
    isin            TEXT    NOT NULL,
    quantity        INTEGER NOT NULL,
    avg_buy_price   REAL    NOT NULL,
    buy_value       REAL    NOT NULL,
    closing_price   REAL,
    closing_value   REAL,
    unrealised_pnl  REAL,
    imported_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(snapshot_date, isin)
);

CREATE TABLE IF NOT EXISTS portfolio_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   DATE    NOT NULL UNIQUE,
    invested_value  REAL,
    closing_value   REAL,
    unrealised_pnl  REAL,
    imported_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _parse_date_text(cell: str) -> str:
    """Extract YYYY-MM-DD from Groww's 'Holdings statement for stocks as on DD-MM-YYYY'."""
    match = re.search(r"(\d{2}-\d{2}-\d{4})", str(cell))
    if not match:
        raise ValueError(f"Could not parse date from: {cell!r}")
    return datetime.strptime(match.group(1), "%d-%m-%Y").strftime("%Y-%m-%d")


def _parse_executed_at(cell: str) -> str:
    """Parse Groww's 'DD-MM-YYYY HH:MM AM/PM' into ISO datetime string."""
    try:
        return datetime.strptime(str(cell).strip(), "%d-%m-%Y %I:%M %p").strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        # fallback: date only
        match = re.search(r"(\d{2}-\d{2}-\d{4})", str(cell))
        if match:
            return datetime.strptime(match.group(1), "%d-%m-%Y").strftime("%Y-%m-%d")
        raise


# ── Holdings importer ─────────────────────────────────────────────────────────

def _import_holdings(path: Path, conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Parse a Groww holdings snapshot xlsx and insert into holdings_snapshots
    and portfolio_summary. Returns (inserted, skipped).

    File layout (0-indexed rows):
      0  Name: <PII>            ← SKIPPED
      1  Unique Client Code: …  ← SKIPPED
      2  blank
      3  "Holdings statement for stocks as on DD-MM-YYYY"
      4  blank
      5  "Summary"
      6  Invested Value  <float>
      7  Closing Value   <float>
      8  Unrealised P&L  <float>
      9  blank
      10 column headers
      11+ data rows
    """
    df = pd.read_excel(path, sheet_name="Sheet1", header=None)

    # --- snapshot date ---
    snapshot_date = _parse_date_text(df.iloc[3, 0])

    # --- portfolio summary ---
    invested = float(df.iloc[6, 1])
    closing  = float(df.iloc[7, 1])
    pnl      = float(df.iloc[8, 1])
    conn.execute(
        "INSERT OR IGNORE INTO portfolio_summary (snapshot_date, invested_value, closing_value, unrealised_pnl) VALUES (?,?,?,?)",
        (snapshot_date, invested, closing, pnl),
    )

    # --- holdings rows (positional columns, header at row 10) ---
    data = df.iloc[11:].copy()
    data.columns = range(len(data.columns))

    inserted = skipped = 0
    for _, row in data.iterrows():
        # skip blank/summary footer rows
        if pd.isna(row[0]) or pd.isna(row[1]):
            continue

        stock_name    = str(row[0]).strip().upper()
        isin          = str(row[1]).strip()
        quantity      = int(row[2])
        avg_buy_price = float(row[3]) if pd.notna(row[3]) else 0.0
        buy_value     = float(row[4]) if pd.notna(row[4]) else 0.0
        closing_price = float(row[5]) if pd.notna(row[5]) else None
        closing_value = float(row[6]) if pd.notna(row[6]) else None
        unrealised    = float(row[7]) if pd.notna(row[7]) else None

        cur = conn.execute(
            """INSERT OR IGNORE INTO holdings_snapshots
               (snapshot_date, stock_name, isin, quantity, avg_buy_price, buy_value,
                closing_price, closing_value, unrealised_pnl)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (snapshot_date, stock_name, isin, quantity, avg_buy_price, buy_value,
             closing_price, closing_value, unrealised),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    return inserted, skipped


# ── Order history importer ────────────────────────────────────────────────────

def _import_orders(path: Path, conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Parse a Groww order history xlsx and insert into transactions.
    Returns (inserted, skipped).

    File layout (0-indexed rows):
      0  Name: <PII>                        ← SKIPPED
      1  Unique Client Code: …              ← SKIPPED
      2  blank
      3  "Order history for stocks from …"
      4  blank
      5  column headers
      6+ data rows
    """
    df = pd.read_excel(path, sheet_name="Sheet1", header=None)

    # row 5 is the header; data starts at row 6
    headers = [str(v).strip() for v in df.iloc[5]]
    data = df.iloc[6:].copy()
    data.columns = headers

    # only keep executed orders (the export may include cancelled/rejected in future)
    data = data[data["Order status"] == "Executed"].copy()

    inserted = skipped = 0
    for _, row in data.iterrows():
        stock_name  = str(row["Stock name"]).strip().upper()
        symbol_raw  = str(row["Symbol"]).strip() if pd.notna(row["Symbol"]) else None
        # BSE scrip codes come through as floats (e.g. 750829.0); clean to int string
        if symbol_raw and re.match(r"^\d+\.0$", symbol_raw):
            symbol_raw = symbol_raw[:-2]  # "750829.0" → "750829"
        symbol      = symbol_raw or None
        isin        = str(row["ISIN"]).strip()
        trade_type  = str(row["Type"]).strip().upper()
        quantity    = int(row["Quantity"])
        total_value = float(row["Value"]) if pd.notna(row["Value"]) else 0.0
        exchange    = str(row["Exchange"]).strip() if pd.notna(row["Exchange"]) else None
        order_id    = str(row["Exchange Order Id"]).strip() if pd.notna(row["Exchange Order Id"]) else None
        executed_at = _parse_executed_at(row["Execution date and time"])

        # zero-value = corporate action (rights entitlement, delisting squeeze-out, etc.)
        is_ca = 1 if total_value == 0.0 else 0
        pps   = (total_value / quantity) if (not is_ca and quantity > 0) else None

        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (stock_name, symbol, isin, trade_type, quantity, total_value,
                price_per_share, exchange, exchange_order_id, executed_at, is_corporate_action)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (stock_name, symbol, isin, trade_type, quantity, total_value,
             pps, exchange, order_id, executed_at, is_ca),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    return inserted, skipped


# ── Auto-detection ────────────────────────────────────────────────────────────

def _detect_files() -> tuple[list[Path], list[Path]]:
    """Scan EXPORT_DIR for holdings and order-history xlsx files."""
    holdings = [Path(p) for p in glob.glob(str(EXPORT_DIR / "*Holding*.xlsx"))]
    orders   = [Path(p) for p in glob.glob(str(EXPORT_DIR / "*Order_History*.xlsx"))]
    # exclude Excel temp files (start with ~$)
    holdings = [p for p in holdings if not p.name.startswith("~$")]
    orders   = [p for p in orders   if not p.name.startswith("~$")]
    return holdings, orders


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import Groww brokerage exports into data/portfolio.db (PII is stripped at parse time)."
    )
    parser.add_argument("--holdings", metavar="PATH", help="Holdings xlsx file")
    parser.add_argument("--orders",   metavar="PATH", help="Order history xlsx file")
    parser.add_argument("--force", action="store_true", help="Reimport even if xlsx files unchanged")
    args = parser.parse_args()

    # All xlsx files in EXPORT_DIR — used for freshness check and state save.
    # Collected here (before any early-exit) so the state save at the end can reference them.
    _all_xlsx = [p for p in EXPORT_DIR.glob("*.xlsx") if not p.name.startswith("~$")]

    # Freshness check: skip if no explicit paths given, --force not set, and files unchanged.
    # Mirrors the pattern in process_insider_trades.py:1623-1637.
    if not (args.holdings or args.orders) and not args.force:
        _xlsx_mtimes = {f.name: f.stat().st_mtime for f in _all_xlsx}
        try:
            _state = json.load(open("data/claude_state.json"))
            _saved = _state.get("portfolio_xlsx_mtimes", {})
        except Exception:
            _saved = {}
        if _xlsx_mtimes == _saved and _all_xlsx:
            print("Portfolio data is current — no new xlsx files. Use --force to reimport.")
            return

    holdings_paths: list[Path] = []
    orders_paths:   list[Path] = []

    if args.holdings or args.orders:
        if args.holdings:
            holdings_paths = [Path(args.holdings)]
        if args.orders:
            orders_paths = [Path(args.orders)]
    else:
        holdings_paths, orders_paths = _detect_files()
        if not holdings_paths and not orders_paths:
            print(f"No xlsx files found in '{EXPORT_DIR}/'. Use --holdings / --orders to specify paths.")
            return

    conn = _connect()

    total_h_ins = total_h_skip = 0
    total_t_ins = total_t_skip = 0

    for p in holdings_paths:
        print(f"Holdings  {p.name} … ", end="", flush=True)
        ins, skip = _import_holdings(p, conn)
        total_h_ins  += ins
        total_h_skip += skip
        print(f"{ins} inserted, {skip} skipped")

    for p in orders_paths:
        print(f"Orders    {p.name} … ", end="", flush=True)
        ins, skip = _import_orders(p, conn)
        total_t_ins  += ins
        total_t_skip += skip
        print(f"{ins} inserted, {skip} skipped")

    conn.close()

    print()
    print("── Summary ──────────────────────────────")
    print(f"  Holdings rows  : {total_h_ins} inserted, {total_h_skip} skipped")
    print(f"  Transactions   : {total_t_ins} inserted, {total_t_skip} skipped")
    print(f"  Database       : {DB_PATH.resolve()}")

    # Save xlsx mtimes so the next auto-detect run can skip if nothing changed.
    try:
        _sp = Path("data/claude_state.json")
        _st = json.load(open(_sp))
        _st["portfolio_xlsx_mtimes"] = {f.name: f.stat().st_mtime for f in _all_xlsx}
        json.dump(_st, open(_sp, "w"), indent=2)
    except Exception as _e:
        print(f"  State save skipped — {_e}")

    # Self-clear the ingestion watch flag — watcher re-sets if new xlsx arrive while we ran
    try:
        _wf = Path("data/watch_flags.json")
        if _wf.exists():
            _fl = json.loads(_wf.read_text())
            _fl["portfolio_xlsx_pending"] = False
            _tmp = _wf.with_suffix(".tmp")
            _tmp.write_text(json.dumps(_fl, indent=2))
            _tmp.rename(_wf)
    except Exception:
        pass


if __name__ == "__main__":
    main()
