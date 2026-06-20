"""
Alert endpoints: list levels, create custom alerts, read alert log.
"""
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from alert_bot.config import (
    STOCKS_DIR, EXCLUDED_MD_FILES, ALERTS_LOG, CUSTOM_ALERTS_FILE,
)
from alert_bot.parser import load_all_stocks
from alert_bot.custom_alerts import CustomAlertsStore, _SIGNAL

router = APIRouter(prefix="/api")

# Shared custom alerts store (same file the bot reads)
_custom_store = CustomAlertsStore(CUSTOM_ALERTS_FILE)


# ── Signal → hex color mapping (matches TUI confidence shading) ──────────

_BUY_COLORS = {
    1: "#1a8a6a",   # muted teal (low confidence)
    2: "#22a882",
    3: "#2dd4a0",   # primary buy color
    4: "#44e0b0",
    5: "#66ecc4",   # vivid (high confidence)
}
_SELL_COLORS = {
    1: "#a04040",   # muted red
    2: "#c85050",
    3: "#f06060",   # primary sell color
    4: "#f88080",   # vivid red
}
_WATCH_COLOR = "#e0c040"  # amber-yellow


def _level_color(alert_type: str, confidence: int) -> str:
    """Return hex color for an alert level based on type and confidence."""
    if alert_type == "BUY":
        return _BUY_COLORS.get(confidence, "#4caf50")
    elif alert_type == "SELL":
        return _SELL_COLORS.get(confidence, "#e53935")
    return _WATCH_COLOR


@router.get("/alerts/{ticker}")
def get_alerts(ticker: str):
    """Return all alert levels (Claude + custom) for a stock."""
    ticker = ticker.upper()

    # Claude alert levels from stocks/TICKER.md
    stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
    stock = next((s for s in stocks if s.ticker == ticker), None)

    levels = []
    if stock:
        for lvl in stock.levels:
            mid = round((lvl.lower + lvl.upper) / 2, 2)
            levels.append({
                "source": "claude",
                "signal": lvl.signal,
                "price_str": lvl.price_str,
                "lower": lvl.lower,
                "upper": lvl.upper,
                "mid": mid,
                "alert_type": lvl.alert_type,
                "message": lvl.message,
                "confidence": lvl.confidence,
                "color": _level_color(lvl.alert_type, lvl.confidence),
            })

    # Custom (user) alert levels
    _custom_store._load()  # re-read file in case bot changed it
    custom_entries = _custom_store.list_alerts(ticker)
    for entry in custom_entries:
        mid = round((entry.lower + entry.upper) / 2, 2)
        levels.append({
            "source": "manual",
            "signal": _SIGNAL.get(entry.alert_type, {}).get(entry.confidence, ""),
            "price_str": entry.price_str,
            "lower": entry.lower,
            "upper": entry.upper,
            "mid": mid,
            "alert_type": entry.alert_type,
            "message": entry.note or f"{ticker} at {entry.price_str}",
            "confidence": entry.confidence,
            "color": _level_color(entry.alert_type, entry.confidence),
        })

    return {"ticker": ticker, "levels": levels}


class CreateAlertRequest(BaseModel):
    price: float
    alert_type: str  # BUY | SELL | WATCH
    confidence: int = 3
    note: str = ""


@router.post("/alerts/{ticker}")
def create_alert(ticker: str, req: CreateAlertRequest):
    """Create a custom alert for a stock."""
    ticker = ticker.upper()

    if req.alert_type not in ("BUY", "SELL", "WATCH"):
        raise HTTPException(400, "alert_type must be BUY, SELL, or WATCH")
    if not 1 <= req.confidence <= 5:
        raise HTTPException(400, "confidence must be 1-5")
    if req.price <= 0:
        raise HTTPException(400, "price must be positive")

    # Build the raw string the parser expects
    raw = f"₹{req.price:.0f} {req.alert_type} {req.confidence}"
    if req.note:
        raw += f" {req.note}"

    entries, errors = _custom_store.parse_entries(ticker, raw)
    if errors:
        raise HTTPException(400, f"Parse error: {'; '.join(errors)}")

    _custom_store.add(ticker, entries)
    return {
        "status": "ok",
        "ticker": ticker,
        "added": len(entries),
        "message": f"Alert at ₹{req.price:.0f} {req.alert_type} added for {ticker}",
    }


@router.get("/alert-log/{ticker}")
def get_alert_log(ticker: str, limit: int = Query(100, ge=1, le=500)):
    """Return recent fired alerts for a stock from alerts.jsonl."""
    ticker = ticker.upper()
    alerts_path = Path(ALERTS_LOG)

    if not alerts_path.exists():
        return {"ticker": ticker, "alerts": []}

    records = []
    try:
        with alerts_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("ticker") == ticker:
                        records.append(r)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {"ticker": ticker, "alerts": []}

    # Return most recent N alerts, newest first
    records = records[-limit:]
    records.reverse()
    return {"ticker": ticker, "alerts": records}
