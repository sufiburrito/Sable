#!/usr/bin/env python3
"""
journal/realized_pnl.py — compute realized P&L from the raw transaction ledger (FIFO).

The broker's tax P&L statement is emailed (not instant), so we compute the same thing
in Python now and store it in a new `closed_lots` table in portfolio.db. FIFO lot
matching — the convention Indian brokers and the tax code use: each SELL is matched
against the earliest unmatched BUY lots, yielding one closed lot per match with its
realized P&L, holding period, and STCG/LTCG class (>12 months = LTCG for listed equity).

GROSS by design: the raw transactions carry no charges (total_value == qty×price), so
this is pre-brokerage/STT/GST/DP. When the broker statement arrives we reconcile against
it (the diff validates this engine) and store the authoritative net. Idempotent — the
table is fully rebuilt from `transactions` each run.

Usage:  python3 -m journal.realized_pnl
"""
import sqlite3
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "portfolio.db"
LTCG_DAYS = 365          # >12 months ≈ >365 days (listed-equity LTCG threshold)


def load_transactions(db_path: Path = DB) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    rows = []
    for sym, tt, qty, price, ts, is_ca in con.execute(
        "SELECT symbol, trade_type, quantity, price_per_share, executed_at, "
        "is_corporate_action FROM transactions"
    ):
        try:
            d = datetime.fromisoformat(str(ts)).date()
        except (ValueError, TypeError):
            d = None
        rows.append({"symbol": str(sym), "trade_type": tt, "quantity": qty,
                     "price": price, "date": d, "is_ca": bool(is_ca)})
    con.close()
    return rows


def compute_closed_lots(txns: list[dict]):
    """FIFO-match SELLs against earlier BUYs per symbol.
    Returns (closed_lots, open_lots, quarantined). Pure — easy to test."""
    quarantined, by_sym = [], defaultdict(list)
    for t in txns:
        if t.get("is_ca") or t.get("price") is None or t.get("quantity") is None or t.get("date") is None:
            quarantined.append({**t, "reason": "corporate_action_or_missing_field"})
        else:
            by_sym[t["symbol"]].append(t)

    closed, open_lots = [], []
    for sym, rows in by_sym.items():
        rows.sort(key=lambda r: r["date"])
        buys: deque = deque()                 # each lot: [buy_date, qty_remaining, buy_price]
        for r in rows:
            if r["trade_type"] == "BUY":
                buys.append([r["date"], r["quantity"], r["price"]])
                continue
            qty = r["quantity"]               # a SELL — consume earliest lots first
            while qty > 0 and buys:
                lot = buys[0]
                m = min(qty, lot[1])
                hd = (r["date"] - lot[0]).days
                closed.append({
                    "symbol": sym, "quantity": m,
                    "buy_date": str(lot[0]), "buy_price": round(lot[2], 2),
                    "sell_date": str(r["date"]), "sell_price": round(r["price"], 2),
                    "realized_pnl": round(m * (r["price"] - lot[2]), 2),
                    "realized_pct": round((r["price"] - lot[2]) / lot[2] * 100, 2),
                    "holding_days": hd, "gain_type": "LTCG" if hd > LTCG_DAYS else "STCG",
                })
                lot[1] -= m
                qty -= m
                if lot[1] == 0:
                    buys.popleft()
            if qty > 0:                       # sold more than we hold on record
                quarantined.append({**r, "reason": "sell_without_matching_buy", "unmatched_qty": qty})
        for lot in buys:
            open_lots.append({"symbol": sym, "quantity": lot[1],
                              "buy_date": str(lot[0]), "buy_price": round(lot[2], 2)})
    return closed, open_lots, quarantined


def write_closed_lots(closed: list[dict], db_path: Path = DB) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute("DROP TABLE IF EXISTS closed_lots")
    con.execute("""
        CREATE TABLE closed_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, quantity INTEGER,
            buy_date TEXT, buy_price REAL, sell_date TEXT, sell_price REAL,
            realized_pnl REAL, realized_pct REAL, holding_days INTEGER,
            gain_type TEXT, basis TEXT DEFAULT 'gross', computed_at TEXT
        )""")
    now = datetime.now().isoformat(timespec="seconds")
    con.executemany(
        "INSERT INTO closed_lots (symbol, quantity, buy_date, buy_price, sell_date, "
        "sell_price, realized_pnl, realized_pct, holding_days, gain_type, computed_at) "
        "VALUES (:symbol,:quantity,:buy_date,:buy_price,:sell_date,:sell_price,"
        ":realized_pnl,:realized_pct,:holding_days,:gain_type,:now)",
        [{**c, "now": now} for c in closed],
    )
    con.commit()
    con.close()


def summarize(closed, open_lots, quarantined) -> str:
    total = sum(c["realized_pnl"] for c in closed)
    wins = [c for c in closed if c["realized_pnl"] > 0]
    stcg = sum(c["realized_pnl"] for c in closed if c["gain_type"] == "STCG")
    ltcg = sum(c["realized_pnl"] for c in closed if c["gain_type"] == "LTCG")
    by_win = sorted(closed, key=lambda c: c["realized_pnl"])
    lines = [
        "💰 **Realized P&L** (GROSS — pre-charges; reconcile with the broker statement)",
        f"Closed lots: {len(closed)}  ·  win-rate {len(wins)/len(closed)*100:.0f}%"
        if closed else "Closed lots: 0",
        f"Total realized: ₹{total:,.0f}   (STCG ₹{stcg:,.0f} · LTCG ₹{ltcg:,.0f})",
        f"Open positions: {len(open_lots)} lots  ·  quarantined (corp-action/oversell): {len(quarantined)}",
    ]
    if closed:
        lines.append("\nTop gains / losses:")
        for c in by_win[-3:][::-1] + by_win[:3]:
            lines.append(f"  {c['symbol']:<12} {c['quantity']:>4} × ₹{c['buy_price']}→₹{c['sell_price']}"
                         f"  ₹{c['realized_pnl']:>9,.0f}  ({c['realized_pct']:+.0f}%, {c['gain_type']}, {c['holding_days']}d)")
    return "\n".join(lines)


def main():
    txns = load_transactions()
    closed, open_lots, quarantined = compute_closed_lots(txns)
    write_closed_lots(closed)
    print(summarize(closed, open_lots, quarantined))
    print(f"\nWrote {len(closed)} closed lots → portfolio.db.closed_lots")


if __name__ == "__main__":
    main()
