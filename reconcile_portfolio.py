"""
reconcile_portfolio.py — match fired alerts against portfolio transactions.

For each alert fired in the last 7 days:
  action_taken  — transaction found matching ticker + direction + price within 1.5×ATR
  skipped       — no transaction AND price moved >0.5×ATR away (opportunity cost)
  pending       — no transaction AND price still within 0.5×ATR (still live)

Writes auto-generated records to data/feedback.jsonl with source="auto_portfolio".
Returns a human-readable digest summary for the morning Telegram digest.

Run nightly after import_portfolio.py so portfolio.db is always fresh first.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytz

logger = logging.getLogger(__name__)

_DATA_DIR = Path("data")
_ANALYSIS_DIR = Path("analysis")
_ALERTS_LOG = _DATA_DIR / "alerts.jsonl"
_FEEDBACK_LOG = _DATA_DIR / "feedback.jsonl"
_DB_PATH = _DATA_DIR / "portfolio.db"
_RECONCILE_STATE = _DATA_DIR / "reconcile_state.json"

IST = pytz.timezone("Asia/Kolkata")

WINDOW_DAYS = 7          # look back this many days for alerts
EXEC_WINDOW_HOURS = 48   # alert and transaction must be within 48h
PRICE_WINDOW_ATR = 1.5   # transaction price must be within 1.5×ATR of alert level
MOVE_ATR = 0.5           # "price moved away" threshold in ATR units


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_alerts(since: datetime) -> list[dict]:
    """Read alerts.jsonl for alerts fired after `since`."""
    if not _ALERTS_LOG.exists():
        return []
    alerts = []
    try:
        with _ALERTS_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("ts") or rec.get("fired_at")
                    if not ts_str:
                        continue
                    # ts can be ISO string or epoch float
                    if isinstance(ts_str, (int, float)):
                        ts = datetime.fromtimestamp(ts_str, tz=IST)
                    else:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = IST.localize(ts)
                        else:
                            ts = ts.astimezone(IST)
                    if ts >= since:
                        rec["_fired_dt"] = ts
                        alerts.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass
    return alerts


def _load_transactions(since: datetime) -> list[dict]:
    """Read transactions from portfolio.db after `since`."""
    if not _DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT symbol, trade_type, price_per_share, quantity, executed_at
            FROM transactions
            WHERE executed_at >= ?
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
        conn.close()
        txns = []
        for r in rows:
            d = dict(r)
            try:
                d["_exec_dt"] = IST.localize(
                    datetime.strptime(d["executed_at"], "%Y-%m-%d %H:%M:%S")
                )
            except ValueError:
                continue
            txns.append(d)
        return txns
    except Exception as e:
        logger.warning(f"reconcile: could not read portfolio.db: {e}")
        return []


def _get_atr(ticker: str) -> float:
    """Read 14-day ATR from OHLC cache. Returns 0 if unavailable."""
    try:
        import pandas as pd
        import numpy as np
        path = _ANALYSIS_DIR / f"{ticker}_ohlc_cache.csv"
        if not path.exists():
            return 0.0
        df = pd.read_csv(path, parse_dates=["Date"]).sort_values("Date").tail(30)
        if len(df) < 15:
            return 0.0
        high = df["High"].values
        low  = df["Low"].values
        close = df["Close"].values
        tr = np.maximum(high[1:] - low[1:],
             np.maximum(np.abs(high[1:] - close[:-1]),
                        np.abs(low[1:] - close[:-1])))
        return float(np.mean(tr[-14:]))
    except Exception:
        return 0.0


def _load_already_reconciled() -> set:
    """Load set of (ticker, price_str, ts) already written to feedback.jsonl."""
    seen: set = set()
    if not _FEEDBACK_LOG.exists():
        return seen
    try:
        with _FEEDBACK_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("source") == "auto_portfolio":
                        key = (
                            rec.get("ticker", ""),
                            rec.get("price_str", ""),
                            rec.get("ts", ""),
                        )
                        seen.add(key)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return seen


def _append_feedback(record: dict) -> None:
    _FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _FEEDBACK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error(f"reconcile: could not write feedback: {e}")


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------

def reconcile() -> list[str]:
    """
    Match alerts to transactions, write feedback.jsonl records, return summary lines.

    Returns a list of human-readable strings suitable for the morning digest.
    """
    since = datetime.now(IST) - timedelta(days=WINDOW_DAYS)
    alerts = _load_alerts(since)
    txns   = _load_transactions(since)
    already = _load_already_reconciled()

    if not alerts:
        return []

    summary_lines: list[str] = []

    for alert in alerts:
        ticker     = alert.get("ticker", "")
        alert_type = alert.get("alert_type", "")
        price_str  = alert.get("price_str", "")
        alert_price = float(alert.get("price", 0.0) or 0.0)
        fired_dt   = alert["_fired_dt"]
        ts_str     = alert.get("ts") or ""

        # Skip duplicates already written in a prior run
        key = (ticker, price_str, str(ts_str))
        if key in already:
            continue

        # Only reconcile BUY and SELL (WATCH has no clear action)
        if alert_type not in ("BUY", "SELL"):
            continue

        atr = _get_atr(ticker)

        # Find matching transaction: same ticker + direction + within 48h + price within 1.5×ATR
        matched_txn = None
        for txn in txns:
            if txn["symbol"] != ticker:
                continue
            if txn["trade_type"] != alert_type:
                continue
            dt_diff = abs((txn["_exec_dt"] - fired_dt).total_seconds() / 3600)
            if dt_diff > EXEC_WINDOW_HOURS:
                continue
            if atr > 0:
                price_diff = abs(txn["price_per_share"] - alert_price)
                if price_diff > PRICE_WINDOW_ATR * atr:
                    continue
            matched_txn = txn
            break

        now = datetime.now(IST)

        if matched_txn:
            # Action taken — log slippage
            exec_price = matched_txn["price_per_share"]
            slippage = (exec_price - alert_price) / alert_price * 100 if alert_price else 0
            sign = "+" if slippage > 0 else ""
            record = {
                "source":       "auto_portfolio",
                "meaning":      "action_taken",
                "emoji":        "👍",
                "ticker":       ticker,
                "alert_type":   alert_type,
                "price_str":    price_str,
                "price":        alert_price,
                "ts":           str(ts_str),
                "reconciled_at": now.isoformat(),
                "exec_price":   exec_price,
                "slippage_pct": round(slippage, 2),
            }
            _append_feedback(record)
            slippage_str = f"{sign}{slippage:.1f}% slippage"
            summary_lines.append(
                f"  ✓ {ticker} {alert_type} ₹{alert_price:,.0f}"
                f" — bought ₹{exec_price:,.0f} ({slippage_str})"
            )
            logger.info(f"reconcile: matched {ticker} {alert_type} — {slippage_str}")

        else:
            # No matching transaction — check if price has moved away
            if atr <= 0:
                continue  # can't determine if "away" without ATR

            # Get current price from OHLC cache (last close — no live fetch)
            try:
                import pandas as pd
                ohlc_path = _ANALYSIS_DIR / f"{ticker}_ohlc_cache.csv"
                if ohlc_path.exists():
                    df = pd.read_csv(ohlc_path, parse_dates=["Date"]).sort_values("Date")
                    curr_price = float(df["Close"].iloc[-1])
                else:
                    curr_price = alert_price  # unknown → treat as still near level
            except Exception:
                curr_price = alert_price

            price_moved = abs(curr_price - alert_price)

            if price_moved < MOVE_ATR * atr:
                # Still near the level — skip (pending)
                continue

            # Price has moved away — opportunity cost
            opp_cost_pct = (curr_price - alert_price) / alert_price * 100 if alert_price else 0
            # For BUY: stock rose without you (positive = missed gain)
            # For SELL: stock fell without you (negative = missed exit)
            if alert_type == "SELL":
                opp_cost_pct = -opp_cost_pct  # invert so positive = missed gain on the exit

            record = {
                "source":        "auto_portfolio",
                "meaning":       "skipped",
                "emoji":         "⏳",
                "ticker":        ticker,
                "alert_type":    alert_type,
                "price_str":     price_str,
                "price":         alert_price,
                "ts":            str(ts_str),
                "reconciled_at": now.isoformat(),
                "curr_price":    curr_price,
                "opp_cost_pct":  round(opp_cost_pct, 2),
            }
            _append_feedback(record)
            sign = "+" if opp_cost_pct > 0 else ""
            summary_lines.append(
                f"  ✗ {ticker} {alert_type} ₹{alert_price:,.0f}"
                f" — not executed · price now ₹{curr_price:,.0f}"
                f" ({sign}{opp_cost_pct:.1f}% missed)"
            )
            logger.info(f"reconcile: skipped {ticker} {alert_type} — opportunity cost {sign}{opp_cost_pct:.1f}%")

    return summary_lines


def format_digest_section(summary_lines: list[str]) -> str | None:
    """Format reconciliation results as a Telegram-ready digest section."""
    if not summary_lines:
        return None
    lines = ["📋 PORTFOLIO ACTIVITY (last 7 days)"] + summary_lines
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = reconcile()
    section = format_digest_section(results)
    if section:
        print(section)
    else:
        print("No actionable alerts to reconcile.")
