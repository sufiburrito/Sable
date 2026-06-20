"""
portfolio_context.py — position-aware line appended to alert messages.

Follows the floor_context.py pattern exactly:
  - One public function: portfolio_hint()
  - Returns str | None — silent None when not held or data unavailable
  - No network calls — reads only data/portfolio.db and stocks/{ticker}.md
  - Any exception → None, never blocks the alert

Example outputs:
  BUY  → "📊 374 shares @ ₹523 avg · +82% at current price · Swing layer: 150 shares"
  SELL → "📊 374 shares @ ₹523 avg · +82% · Swing layer: 150 shares available to trim"
  Not held → None (silent)
"""

import re
import sqlite3
from pathlib import Path

_DB_PATH = Path("data/portfolio.db")
_STOCKS_DIR = Path("stocks")

# ── Core % lookup ─────────────────────────────────────────────────────────────

def _core_pct(ticker: str) -> int:
    """Read Core Position % from stocks/{ticker}.md. Returns 0 if absent."""
    md = _STOCKS_DIR / f"{ticker}.md"
    if not md.exists():
        return 0
    try:
        text = md.read_text(encoding="utf-8")
        m = re.search(r"\*\*Core Position:\*\*\s*(\d+)%", text)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


# ── Position lookup ───────────────────────────────────────────────────────────

def _get_position(ticker: str) -> dict | None:
    """
    Query portfolio.db for the latest holdings snapshot for this ticker.
    Returns dict with quantity, avg_buy_price or None if not held.

    Uses transactions (symbol → ISIN) joined with holdings_snapshots (ISIN → data)
    because holdings_snapshots has no symbol column.
    """
    if not _DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT h.quantity, h.avg_buy_price
            FROM holdings_snapshots h
            JOIN transactions t ON h.isin = t.isin
            WHERE t.symbol = ?
              AND h.snapshot_date = (SELECT MAX(snapshot_date) FROM holdings_snapshots)
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


# ── Public function ───────────────────────────────────────────────────────────

def portfolio_fragment(ticker: str, curr_price: float) -> str | None:
    """
    Compact position fragment for the merged 📊 context line, e.g.
    "Holding 40 @ ₹676 (−9%)", or None if not held. "Holding" labels it as the
    user's own position (unambiguous next to a BUY/SELL header). The verbose
    swing/core breakdown lives in the thesis line.
    """
    try:
        pos = _get_position(ticker)
        if pos is None:
            return None
        qty = pos["quantity"]
        avg = pos["avg_buy_price"]
        pnl = ((curr_price - avg) / avg * 100) if avg > 0 else 0.0
        sign = "+" if pnl >= 0 else "−"
        return f"Holding {qty} @ ₹{avg:,.0f} ({sign}{abs(pnl):.0f}%)"
    except Exception:
        return None


def portfolio_hint(ticker: str, alert_type: str, curr_price: float) -> str | None:
    """
    Returns a one-line position summary if the user holds this stock, else None.

    Called in main.py's alert dispatch loop after floor_hint, before notifier.send().
    Failure is always silent — a crash here must never block the core alert.
    """
    try:
        pos = _get_position(ticker)
        if pos is None:
            return None

        qty       = pos["quantity"]
        avg       = pos["avg_buy_price"]
        core_pct  = _core_pct(ticker)
        core_qty  = round(qty * core_pct / 100)
        swing_qty = qty - core_qty

        # P&L % at current price (not at snapshot closing price — uses live price)
        pnl_pct = ((curr_price - avg) / avg * 100) if avg > 0 else 0.0
        pnl_sign = "+" if pnl_pct >= 0 else ""

        base = f"📊 {qty} shares @ ₹{avg:,.0f} avg · {pnl_sign}{pnl_pct:.0f}% at current price"

        alert_upper = alert_type.upper()

        if alert_upper in ("BUY", "WATCH"):
            if swing_qty <= 0:
                layer = "Position is fully core — adding extends core"
            else:
                layer = f"Swing layer: {swing_qty} shares"

        elif alert_upper == "SELL":
            if swing_qty <= 0:
                layer = "Core-only position — trim only if thesis broken"
            else:
                layer = f"Swing layer: {swing_qty} shares available to trim"

        else:
            layer = f"Core {core_qty} / Swing {swing_qty}"

        return f"{base} · {layer}"

    except Exception:
        return None
