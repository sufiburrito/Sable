#!/usr/bin/env python3
"""
market_db.py — Rolling SQLite store for NSE public market data.

Tables: insider_trades, bulk_deals, promoter_holdings, fundamentals, party_profiles
Strategy: dual-write + derived views. Writers write to DB first, then regenerate
existing JSON/CSV outputs as derived views from DB queries. Existing readers keep
working unchanged. Readers migrate to DB queries one at a time.

Usage:
    python3 market_db.py          # create schema + run one-time CSV migration
    python3 market_db.py --stats  # print row counts per table
    python3 market_db.py --force  # re-run migration even if sentinel exists
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

DB_PATH      = Path("data/market.db")
ANALYSIS_DIR = Path("analysis")
DATA_DIR     = Path("data")
SENTINEL     = DATA_DIR / "market_db_initialized"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS insider_trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                DATE    NOT NULL,
            ticker              TEXT    NOT NULL,
            party_name          TEXT    NOT NULL,
            category            TEXT,
            tier                TEXT,
            trade_type          TEXT    NOT NULL,
            quantity            INTEGER,
            value_cr            REAL,
            avg_price           REAL,
            holdings_change_pct REAL,
            source_file         TEXT,
            imported_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, ticker, party_name, quantity, trade_type)
        );
        CREATE INDEX IF NOT EXISTS idx_it_ticker_date ON insider_trades(ticker, date);
        CREATE INDEX IF NOT EXISTS idx_it_tier        ON insider_trades(tier, trade_type, date);

        CREATE TABLE IF NOT EXISTS bulk_deals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        DATE    NOT NULL,
            ticker      TEXT    NOT NULL,
            client_name TEXT    NOT NULL,
            trade_type  TEXT    NOT NULL,
            quantity    INTEGER,
            price       REAL,
            deal_type   TEXT    NOT NULL,
            imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, ticker, client_name, quantity, deal_type)
        );
        CREATE INDEX IF NOT EXISTS idx_bd_ticker_date ON bulk_deals(ticker, date);

        CREATE TABLE IF NOT EXISTS promoter_holdings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            quarter_date  DATE    NOT NULL,
            ticker        TEXT    NOT NULL,
            promoter_pct  REAL,
            fii_pct       REAL,
            dii_pct       REAL,
            public_pct    REAL,
            shareholders  INTEGER,
            imported_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quarter_date, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_ph_ticker ON promoter_holdings(ticker, quarter_date);

        CREATE TABLE IF NOT EXISTS fundamentals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker              TEXT    NOT NULL,
            period              TEXT    NOT NULL,
            period_type         TEXT    NOT NULL,
            revenue_cr          REAL,
            net_profit_cr       REAL,
            roce_pct            REAL,
            roe_pct             REAL,
            debt_equity         REAL,
            promoter_pledge_pct REAL,
            pe_ratio            REAL,
            pb_ratio            REAL,
            fetched_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, period, period_type)
        );
        CREATE INDEX IF NOT EXISTS idx_fund_ticker ON fundamentals(ticker, period);

        CREATE TABLE IF NOT EXISTS party_profiles (
            party_name          TEXT    PRIMARY KEY,
            tier                TEXT,
            category            TEXT,
            confidence          TEXT,
            confidence_rationale TEXT,
            who                 TEXT,
            track_record        TEXT,
            pattern             TEXT,
            total_buy_value_cr  REAL    DEFAULT 0,
            total_sell_value_cr REAL    DEFAULT 0,
            trade_count         INTEGER DEFAULT 0,
            first_trade_date    DATE,
            last_trade_date     DATE,
            last_researched     TEXT,
            tickers_traded      TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_fii_dii (
            date         DATE    PRIMARY KEY,
            fii_net_cr   REAL,          -- daily net flow (negative = outflow)
            dii_net_cr   REAL,
            fii_buy_cr   REAL,
            fii_sell_cr  REAL,
            dii_buy_cr   REAL,
            dii_sell_cr  REAL,
            fii_mtd_cr   REAL,          -- month-to-date cumulative (NULL if not available)
            dii_mtd_cr   REAL,
            source       TEXT,          -- "NSE-playwright" or "manual-digest"
            imported_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Write helpers — each accepts a dict with either the DB column names
# or the internal process_insider_trades.py field names (both work).
# ---------------------------------------------------------------------------

def insert_insider_trade(conn: sqlite3.Connection, row: dict):
    """INSERT OR IGNORE — UNIQUE constraint deduplicates automatically."""
    conn.execute("""
        INSERT OR IGNORE INTO insider_trades
            (date, ticker, party_name, category, tier, trade_type,
             quantity, value_cr, avg_price, holdings_change_pct, source_file)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        row.get("date"),
        row.get("ticker"),
        row.get("party") or row.get("party_name"),
        row.get("category"),
        row.get("tier"),
        row.get("type") or row.get("trade_type"),
        row.get("qty") or row.get("quantity"),
        row.get("value_cr"),
        row.get("avg_price"),
        row.get("holdings_change_pct"),
        row.get("source_file"),
    ))


def upsert_bulk_deal(conn: sqlite3.Connection, row: dict):
    """INSERT OR IGNORE — safe to call repeatedly (idempotent)."""
    conn.execute("""
        INSERT OR IGNORE INTO bulk_deals
            (date, ticker, client_name, trade_type, quantity, price, deal_type)
        VALUES (?,?,?,?,?,?,?)
    """, (
        row.get("date"),
        row.get("ticker"),
        row.get("client_name"),
        row.get("trade_type"),
        row.get("quantity"),
        row.get("price"),
        row.get("deal_type", "bulk"),
    ))


def upsert_promoter_holding(conn: sqlite3.Connection, row: dict):
    """INSERT OR REPLACE — quarterly data can be refreshed in-place."""
    conn.execute("""
        INSERT OR REPLACE INTO promoter_holdings
            (quarter_date, ticker, promoter_pct, fii_pct, dii_pct, public_pct, shareholders)
        VALUES (?,?,?,?,?,?,?)
    """, (
        row.get("quarter_date"),
        row.get("ticker"),
        row.get("promoter_pct"),
        row.get("fii_pct"),
        row.get("dii_pct"),
        row.get("public_pct"),
        row.get("shareholders"),
    ))


def upsert_fundamentals(conn: sqlite3.Connection, row: dict):
    """INSERT OR REPLACE — re-fetch updates in-place."""
    conn.execute("""
        INSERT OR REPLACE INTO fundamentals
            (ticker, period, period_type, revenue_cr, net_profit_cr,
             roce_pct, roe_pct, debt_equity, promoter_pledge_pct,
             pe_ratio, pb_ratio, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
    """, (
        row.get("ticker"),
        row.get("period"),
        row.get("period_type", "quarterly"),
        row.get("revenue_cr"),
        row.get("net_profit_cr"),
        row.get("roce_pct"),
        row.get("roe_pct"),
        row.get("debt_equity"),
        row.get("promoter_pledge_pct"),
        row.get("pe_ratio"),
        row.get("pb_ratio"),
    ))


def is_fundamentals_fresh(conn: sqlite3.Connection, ticker: str, ttl_days: int = 60) -> bool:
    """Return True if fundamentals for ticker were fetched within ttl_days."""
    row = conn.execute("""
        SELECT fetched_at FROM fundamentals
        WHERE ticker = ? ORDER BY fetched_at DESC LIMIT 1
    """, (ticker,)).fetchone()
    if not row:
        return False
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
        return (datetime.utcnow() - fetched).days < ttl_days
    except (ValueError, TypeError):
        return False


def upsert_party_profile(conn: sqlite3.Connection, name: str, data: dict):
    """Update existing profile or insert new one."""
    tickers_json = json.dumps(list(data.get("stocks_traded") or data.get("tickers_traded") or []))
    existing = conn.execute(
        "SELECT 1 FROM party_profiles WHERE party_name = ?", (name,)
    ).fetchone()
    if existing:
        conn.execute("""
            UPDATE party_profiles SET
                tier=?, category=?, confidence=?, confidence_rationale=?,
                who=?, track_record=?, pattern=?,
                total_buy_value_cr=?, total_sell_value_cr=?, trade_count=?,
                first_trade_date=?, last_trade_date=?, last_researched=?,
                tickers_traded=?
            WHERE party_name=?
        """, (
            data.get("tier"), data.get("category"),
            data.get("confidence"), data.get("confidence_rationale"),
            data.get("who"), data.get("track_record"), data.get("pattern"),
            data.get("total_buy_value_cr", 0) or 0,
            data.get("total_sell_value_cr", 0) or 0,
            data.get("trade_count", 0) or 0,
            data.get("first_trade_date"), data.get("last_trade_date"),
            data.get("last_researched"), tickers_json, name,
        ))
    else:
        conn.execute("""
            INSERT INTO party_profiles
                (party_name, tier, category, confidence, confidence_rationale,
                 who, track_record, pattern, total_buy_value_cr, total_sell_value_cr,
                 trade_count, first_trade_date, last_trade_date, last_researched, tickers_traded)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            name, data.get("tier"), data.get("category"),
            data.get("confidence"), data.get("confidence_rationale"),
            data.get("who"), data.get("track_record"), data.get("pattern"),
            data.get("total_buy_value_cr", 0) or 0,
            data.get("total_sell_value_cr", 0) or 0,
            data.get("trade_count", 0) or 0,
            data.get("first_trade_date"), data.get("last_trade_date"),
            data.get("last_researched"), tickers_json,
        ))


# ---------------------------------------------------------------------------
# Query helpers — return shapes compatible with existing code
# ---------------------------------------------------------------------------

def query_insider_summary(conn: sqlite3.Connection, ticker: str,
                          days: int = 30) -> Optional[dict]:
    """
    Return a dict matching insider_activity.json portfolio_activity[ticker] shape.
    Drop-in replacement for confidence.py Factor 8 JSON read (~3 lines to migrate).

    Returns None if no trades found in the window.
    """
    rows = conn.execute("""
        SELECT party_name, tier, trade_type, value_cr, avg_price,
               holdings_change_pct, date, quantity
        FROM insider_trades
        WHERE ticker = ? AND date >= date('now', ?)
        ORDER BY date DESC
    """, (ticker, f"-{days} days")).fetchall()

    if not rows:
        return None

    trades = [dict(r) for r in rows]
    buy_trades  = [t for t in trades if t["trade_type"] == "buy"]
    sell_trades = [t for t in trades if t["trade_type"] == "sell"]
    total_buy   = sum(t["value_cr"] or 0 for t in buy_trades)
    total_sell  = sum(t["value_cr"] or 0 for t in sell_trades)

    return {
        "trades": trades,
        "summary": {
            "net_direction":       "buy" if total_buy >= total_sell else "sell",
            "promoter_buying":     any(t["tier"] == "promoter" and t["trade_type"] == "buy"  for t in trades),
            "promoter_selling":    any(t["tier"] == "promoter" and t["trade_type"] == "sell" for t in trades),
            "total_buy_value_cr":  round(total_buy, 2),
            "total_sell_value_cr": round(total_sell, 2),
            "trade_count":         len(trades),
            "latest_trade_date":   max(t["date"] for t in trades) if trades else "",
        },
    }


def query_promoter_trend(conn: sqlite3.Connection, ticker: str,
                         n_quarters: int = 8) -> Optional[pd.DataFrame]:
    """
    Return a DataFrame matching analysis/TICKER_promoter.csv shape.
    Drop-in replacement for retrospective_analysis.py CSV read (~3 lines to migrate).

    Index = quarter-end dates (datetime), columns = promoter_pct, fii_pct,
    dii_pct, public_pct, shareholders. Returns None if no rows found.
    """
    rows = conn.execute("""
        SELECT quarter_date, promoter_pct, fii_pct, dii_pct, public_pct, shareholders
        FROM promoter_holdings
        WHERE ticker = ?
        ORDER BY quarter_date ASC
        LIMIT ?
    """, (ticker, n_quarters)).fetchall()

    if not rows:
        return None

    df = pd.DataFrame([dict(r) for r in rows])
    df["quarter_date"] = pd.to_datetime(df["quarter_date"])
    df = df.set_index("quarter_date")
    df.index.name = "date"
    return df


def query_windowed_trades(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """
    Return all insider trades from the last `days` days, normalized to the
    internal process_insider_trades.py field names so _build_narratives() and
    _build_sector_signals() can consume them unchanged.
    """
    rows = conn.execute("""
        SELECT date, ticker, party_name, category, tier, trade_type,
               quantity, value_cr, avg_price, holdings_change_pct, source_file
        FROM insider_trades
        WHERE date >= date('now', ?)
        ORDER BY date DESC
    """, (f"-{days} days",)).fetchall()

    result = []
    for r in rows:
        result.append({
            "ticker":               r["ticker"],
            "stock_name":           r["ticker"],   # original company name not stored; ticker is fine
            "date":                 r["date"],
            "party":                r["party_name"],
            "category":             r["category"],
            "tier":                 r["tier"],
            "type":                 r["trade_type"],
            "qty":                  r["quantity"],
            "value":                (r["value_cr"] or 0) * 1e7,  # back to rupees if needed
            "value_cr":             r["value_cr"],
            "avg_price":            r["avg_price"],
            "holdings_change_pct":  r["holdings_change_pct"],
            "source_file":          r["source_file"],
        })
    return result


# ---------------------------------------------------------------------------
# Phase 2 cross-table query helpers
# ---------------------------------------------------------------------------

def query_promoter_bulk_convergence(conn: sqlite3.Connection,
                                    days: int = 30) -> list[dict]:
    """
    Promoter insider buy + a bulk/block deal on the same ticker within 7 days.
    This is the highest-confidence smart money signal — requires a cross-table JOIN
    that was impossible from flat JSONs.
    Returns [] if no convergence events found.
    """
    rows = conn.execute("""
        SELECT it.ticker,
               it.party_name,
               ROUND(it.value_cr, 1)   AS insider_cr,
               it.date                 AS insider_date,
               bd.client_name,
               bd.quantity             AS bulk_qty,
               bd.deal_type,
               bd.date                 AS bulk_date
        FROM insider_trades it
        JOIN bulk_deals bd ON it.ticker = bd.ticker
        WHERE it.tier       = 'promoter'
          AND it.trade_type = 'buy'
          AND bd.date BETWEEN it.date AND date(it.date, '+7 days')
          AND it.date >= date('now', ?)
        ORDER BY it.value_cr DESC
    """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def query_coordinated_buying(conn: sqlite3.Connection,
                              days: int = 14,
                              min_entities: int = 3) -> list[dict]:
    """
    Tickers where 3+ distinct entities bought within the window.
    Detects organised accumulation before it shows in price action.
    """
    rows = conn.execute("""
        SELECT ticker,
               COUNT(DISTINCT party_name) AS entity_count,
               ROUND(SUM(value_cr), 1)    AS total_cr,
               MIN(date)                  AS first_buy,
               MAX(date)                  AS last_buy
        FROM insider_trades
        WHERE trade_type = 'buy'
          AND date >= date('now', ?)
        GROUP BY ticker
        HAVING entity_count >= ?
        ORDER BY total_cr DESC
    """, (f"-{days} days", min_entities)).fetchall()
    return [dict(r) for r in rows]


def query_sector_smart_money(conn: sqlite3.Connection,
                              days: int = 30,
                              limit: int = 20) -> list[dict]:
    """
    Per-ticker buy flow aggregated by tier, last `days` days.
    Used by CONVERGENCE_PROMPT.md as a replacement for the sector_signals.json
    smart money layer when running sqlite3 shell queries.
    """
    rows = conn.execute("""
        SELECT ticker,
               tier,
               ROUND(SUM(value_cr), 1)    AS flow_cr,
               COUNT(DISTINCT party_name) AS participants
        FROM insider_trades
        WHERE trade_type = 'buy'
          AND date >= date('now', ?)
          AND tier IN ('promoter', 'director', 'bulk', 'block')
        GROUP BY ticker, tier
        ORDER BY flow_cr DESC
        LIMIT ?
    """, (f"-{days} days", limit)).fetchall()
    return [dict(r) for r in rows]


def query_pledge_risk(conn: sqlite3.Connection,
                      min_pct: float = 25.0) -> list[dict]:
    """
    Tickers where promoter_pledge_pct exceeds min_pct in the most recent period.
    JOINs promoter_holdings to surface pledge + declining holding together —
    the combination that precedes forced selling.
    Returns [] if fundamentals table is empty or pledge data not yet fetched.
    """
    rows = conn.execute("""
        SELECT f.ticker,
               ROUND(f.promoter_pledge_pct, 1) AS pledge_pct,
               f.period,
               ROUND(ph.promoter_pct, 2)       AS promoter_holding_pct
        FROM fundamentals f
        JOIN promoter_holdings ph ON f.ticker = ph.ticker
        WHERE f.promoter_pledge_pct > ?
          AND f.period = (
              SELECT MAX(period) FROM fundamentals WHERE ticker = f.ticker
          )
          AND ph.quarter_date = (
              SELECT MAX(quarter_date) FROM promoter_holdings WHERE ticker = ph.ticker
          )
        ORDER BY f.promoter_pledge_pct DESC
    """, (min_pct,)).fetchall()
    return [dict(r) for r in rows]


def query_explore_candidates(conn: sqlite3.Connection,
                              days: int = 30,
                              min_value_cr: float = 5.0,
                              portfolio_tickers: "list[str] | None" = None) -> list[dict]:
    """
    Non-portfolio stocks with recent smart money buy activity.
    Returns a list of dicts matching insider_activity.json explore_candidates shape:
      {ticker, stock_name, value_cr, reason, narrative}
    so discovery_scanner.py's scoring loop works unchanged.
    """
    exclude = tuple(portfolio_tickers or [])
    placeholder = ("NOT IN (" + ",".join("?" * len(exclude)) + ")"
                   if exclude else "NOT IN ('')")

    rows = conn.execute(f"""
        SELECT ticker,
               tier,
               ROUND(SUM(value_cr), 2)    AS total_value,
               COUNT(DISTINCT party_name) AS party_count,
               MAX(date)                  AS latest_trade
        FROM insider_trades
        WHERE trade_type = 'buy'
          AND date >= date('now', ?)
          AND ticker {placeholder}
        GROUP BY ticker, tier
        HAVING total_value >= ?
        ORDER BY total_value DESC
    """, (f"-{days} days", *exclude, min_value_cr)).fetchall()

    candidates = []
    for r in rows:
        row = dict(r)
        reason = ("promoter_accumulation"
                  if row["tier"] in ("promoter", "director")
                  else "coordinated_institutional")
        entity_word = "entity" if row["party_count"] == 1 else "entities"
        candidates.append({
            "ticker":     row["ticker"],
            "stock_name": row["ticker"],
            "value_cr":   row["total_value"],
            "reason":     reason,
            "narrative":  (f"₹{row['total_value']:.0f} Cr {row['tier']} buying "
                           f"({row['party_count']} {entity_word} since {row['latest_trade']})"),
        })
    return candidates


def query_entity_counts(conn: sqlite3.Connection,
                        days: int = 30) -> dict[str, int]:
    """
    Map ticker → number of distinct buying entities within window.
    Drop-in replacement for discovery_scanner._build_entity_counts().
    """
    rows = conn.execute("""
        SELECT ticker, COUNT(DISTINCT party_name) AS entity_count
        FROM insider_trades
        WHERE trade_type = 'buy' AND date >= date('now', ?)
        GROUP BY ticker
    """, (f"-{days} days",)).fetchall()
    return {r["ticker"]: r["entity_count"] for r in rows}


def query_net_accumulation(conn: sqlite3.Connection,
                           ticker: str,
                           days: int = 60) -> dict:
    """
    Net position analysis for a single ticker: breaks down gross buying into
    genuine accumulation vs matched arbitrage activity.

    Returns:
        net_value_cr        — sum of (buys - sells) across all entities
        genuine_accumulators — list of {party_name, net_cr} for entities that
                               have buys but either zero sells, or net_cr > 5%
                               of their gross_buy (i.e. a directional position)
        arbitrage_ratio     — fraction of gross buying from matched-pair entities;
                               high values (>0.8) indicate the gross signal is
                               dominated by round-trip trading, not accumulation

    Note: genuine_accumulator status reflects trades within the query window only.
    An entity that bought in an earlier CSV batch and sells in a later one will
    appear genuinely long until both batches fall in the same window.
    """
    rows = conn.execute("""
        SELECT party_name,
               SUM(CASE WHEN trade_type = 'buy'  THEN value_cr ELSE 0 END) AS gross_buy,
               SUM(CASE WHEN trade_type = 'sell' THEN value_cr ELSE 0 END) AS gross_sell
        FROM   insider_trades
        WHERE  ticker = ? AND date >= date('now', ?)
        GROUP  BY party_name
    """, (ticker, f"-{days} days")).fetchall()

    if not rows:
        return {"net_value_cr": 0.0, "genuine_accumulators": [], "arbitrage_ratio": 0.0}

    total_gross_buy = 0.0
    total_net       = 0.0
    genuine         = []
    arb_gross_buy   = 0.0

    for r in rows:
        gross_buy  = r["gross_buy"]  or 0.0
        gross_sell = r["gross_sell"] or 0.0
        net_cr     = gross_buy - gross_sell
        total_gross_buy += gross_buy
        total_net       += net_cr

        if gross_buy > 0:
            is_genuine = (gross_sell == 0) or (net_cr > 0.05 * gross_buy)
            if is_genuine:
                genuine.append({"party_name": r["party_name"], "net_cr": round(net_cr, 2)})
            else:
                arb_gross_buy += gross_buy

    genuine.sort(key=lambda x: -x["net_cr"])
    arbitrage_ratio = (arb_gross_buy / total_gross_buy) if total_gross_buy > 0 else 0.0

    return {
        "net_value_cr":        round(total_net, 2),
        "genuine_accumulators": genuine,
        "arbitrage_ratio":     round(arbitrage_ratio, 3),
    }


# ---------------------------------------------------------------------------
# FII/DII daily flows
# ---------------------------------------------------------------------------

def upsert_fii_dii(conn: sqlite3.Connection, row: dict):
    """
    INSERT OR REPLACE for one day's FII/DII data.
    Idempotent — re-running with the same date updates the row in-place.
    """
    conn.execute("""
        INSERT OR REPLACE INTO daily_fii_dii
            (date, fii_net_cr, dii_net_cr, fii_buy_cr, fii_sell_cr,
             dii_buy_cr, dii_sell_cr, fii_mtd_cr, dii_mtd_cr, source)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        row.get("date"),
        row.get("fii_net_cr"),
        row.get("dii_net_cr"),
        row.get("fii_buy_cr"),
        row.get("fii_sell_cr"),
        row.get("dii_buy_cr"),
        row.get("dii_sell_cr"),
        row.get("fii_mtd_cr"),
        row.get("dii_mtd_cr"),
        row.get("source", "NSE-playwright"),
    ))


def query_fii_dii(conn: sqlite3.Connection,
                  days: int = 30) -> list[dict]:
    """Return last N days of FII/DII flows ordered by date DESC."""
    rows = conn.execute("""
        SELECT date, fii_net_cr, dii_net_cr, fii_buy_cr, fii_sell_cr,
               dii_buy_cr, dii_sell_cr, fii_mtd_cr, dii_mtd_cr, source
        FROM daily_fii_dii
        WHERE date >= date('now', ?)
        ORDER BY date DESC
    """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# One-time migration helpers
# ---------------------------------------------------------------------------

def import_existing_csvs(conn: sqlite3.Connection):
    """Backfill promoter_holdings and bulk_deals from existing analysis/ CSVs."""
    promoter_count = 0
    bulk_count = 0

    for csv_path in sorted(ANALYSIS_DIR.glob("*_promoter.csv")):
        ticker = csv_path.stem.replace("_promoter", "")
        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            for qdate, row in df.iterrows():
                upsert_promoter_holding(conn, {
                    "quarter_date": qdate.date().isoformat(),
                    "ticker":       ticker,
                    "promoter_pct": row.get("promoter_pct"),
                    "fii_pct":      row.get("fii_pct"),
                    "dii_pct":      row.get("dii_pct"),
                    "public_pct":   row.get("public_pct"),
                    "shareholders": row.get("shareholders"),
                })
                promoter_count += 1
        except Exception as e:
            print(f"  WARNING: {csv_path.name} — {e}")

    for csv_path in sorted(ANALYSIS_DIR.glob("*_bulkdeals.csv")):
        ticker = csv_path.stem.replace("_bulkdeals", "")
        try:
            df = pd.read_csv(csv_path)
            for _, row in df.iterrows():
                date_raw = row.get("Date", "")
                try:
                    date_str = pd.to_datetime(date_raw).date().isoformat()
                except Exception:
                    date_str = str(date_raw)
                buy_sell = str(row.get("Buy/Sell", "")).strip().lower()
                upsert_bulk_deal(conn, {
                    "date":        date_str,
                    "ticker":      ticker,
                    "client_name": str(row.get("Client Name", "") or ""),
                    "trade_type":  "buy" if buy_sell.startswith("b") else "sell",
                    "quantity":    int(float(str(row.get("Quantity Traded", 0) or 0))),
                    "price":       float(str(row.get("Trade Price / Wght. Avg. Price", 0) or 0).replace(",", "") or 0),
                    "deal_type":   "bulk",
                })
                bulk_count += 1
        except Exception as e:
            print(f"  WARNING: {csv_path.name} — {e}")

    conn.commit()
    print(f"  Migration: {promoter_count} promoter rows, {bulk_count} bulk deal rows imported")


def import_existing_profiles(conn: sqlite3.Connection):
    """Backfill party_profiles from data/insider_profiles.json."""
    profiles_path = DATA_DIR / "insider_profiles.json"
    if not profiles_path.exists():
        print("  No insider_profiles.json — skipping party profiles migration")
        return
    try:
        profiles = json.loads(profiles_path.read_text())
    except Exception as e:
        print(f"  WARNING: insider_profiles.json — {e}")
        return
    for name, data in profiles.items():
        upsert_party_profile(conn, name, data)
    conn.commit()
    print(f"  Migration: {len(profiles)} party profiles imported")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    force = "--force" in sys.argv

    conn = get_conn()

    if "--stats" in sys.argv:
        for table in ("insider_trades", "bulk_deals", "promoter_holdings",
                      "fundamentals", "party_profiles"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:25s}: {n:>6} rows")
        conn.close()
        sys.exit(0)

    if SENTINEL.exists() and not force:
        print("market.db already initialized. Use --stats or --force to re-migrate.")
        conn.close()
        sys.exit(0)

    print("Running one-time CSV migration into market.db...")
    import_existing_csvs(conn)
    import_existing_profiles(conn)

    SENTINEL.parent.mkdir(exist_ok=True)
    SENTINEL.write_text(datetime.utcnow().isoformat())
    print(f"Done. Sentinel written to {SENTINEL}")
    conn.close()
