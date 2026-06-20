"""
Market Mood Index (MMI) fetcher for TickerTape.

Data is embedded as server-side JSON in the page HTML (__NEXT_DATA__),
so no API key or browser automation is needed.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_URL = "https://www.tickertape.in/market-mood-index"
_TIMEOUT = 15


@dataclass
class MMISnapshot:
    value: float
    zone: str         # "Extreme Fear" | "Fear" | "Greed" | "Extreme Greed"
    last_day: float
    last_week: float
    last_month: float


def fetch_mmi() -> Optional[MMISnapshot]:
    """Fetch current MMI from TickerTape. Returns None on any failure."""
    try:
        resp = requests.get(_URL, timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL
        )
        if not match:
            logger.warning("MMI: could not find __NEXT_DATA__ in page")
            return None

        page_data = json.loads(match.group(1))
        now = page_data["props"]["pageProps"]["nowData"]

        value = float(now["currentValue"])
        return MMISnapshot(
            value=value,
            zone=classify(value),
            last_day=float(now["lastDay"]["indicator"]),
            last_week=float(now["lastWeek"]["indicator"]),
            last_month=float(now["lastMonth"]["indicator"]),
        )
    except Exception as e:
        logger.error(f"MMI fetch failed: {e}")
        return None


_ZONE_EMOJI = {
    "Extreme Fear":  "🟢",
    "Fear":          "🟡",
    "Greed":         "🟠",
    "Extreme Greed": "🔴",
}


def classify(value: float) -> str:
    # TickerTape official scale: <30 Extreme Fear, 30-50 Fear, 50-70 Greed, ≥70 Extreme Greed
    # If this ever drifts out of sync, check nowData["sentiment"] (or similar field) in
    # __NEXT_DATA__ — TickerTape likely embeds the zone label directly and we could read
    # it from there instead of computing it ourselves.
    if value < 30:
        return "Extreme Fear"
    elif value < 50:
        return "Fear"
    elif value < 70:
        return "Greed"
    else:
        return "Extreme Greed"


def format_pin(snap: MMISnapshot) -> str:
    """Short one-liner for the compact pinned message."""
    emoji = _ZONE_EMOJI.get(snap.zone, "⚪")
    return f"{emoji} <b>MMI: {snap.value:.1f} — {snap.zone}</b>"


def format_telegram(snap: MMISnapshot, prev_value: Optional[float] = None) -> str:
    emoji = _ZONE_EMOJI.get(snap.zone, "⚪")
    delta = ""
    if prev_value is not None:
        diff = snap.value - prev_value
        arrow = "↑" if diff > 0 else "↓"
        delta = f"  {arrow} {abs(diff):.1f} pts from {prev_value:.1f}"

    return (
        f"{emoji} <b>MMI: {snap.value:.1f} — {snap.zone}</b>{delta}\n"
        f"Yesterday: {snap.last_day:.1f}  |  "
        f"Last week: {snap.last_week:.1f}  |  "
        f"Last month: {snap.last_month:.1f}"
    )
