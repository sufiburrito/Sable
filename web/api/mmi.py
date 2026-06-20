"""
Market Mood Index endpoint.
Reuses the bot's MMI scraper.
"""
from fastapi import APIRouter

from alert_bot.mmi import fetch_mmi

router = APIRouter(prefix="/api")


@router.get("/mmi")
def get_mmi():
    """Return current Market Mood Index snapshot."""
    snap = fetch_mmi()
    if snap is None:
        return {"available": False}

    return {
        "available": True,
        "value": round(snap.value, 1),
        "zone": snap.zone,
        "last_day": round(snap.last_day, 1),
        "last_week": round(snap.last_week, 1),
        "last_month": round(snap.last_month, 1),
        "day_delta": round(snap.value - snap.last_day, 1),
        "week_delta": round(snap.value - snap.last_week, 1),
    }
