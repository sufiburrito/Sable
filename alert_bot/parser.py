"""
Parses stock .md files into StockConfig dataclasses.

To add a new stock: copy _TEMPLATE.md, fill it in, drop it in the stocks directory.
The bot picks it up automatically on the next daily reload (or restart).
"""
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


# Confidence score per signal emoji (1 = lowest conviction, 5 = highest)
SIGNAL_CONFIDENCE: dict[str, int] = {
    # BUY — weakest to strongest
    "🟡": 1, "🟢": 2, "🔵": 3, "🟠": 4, "🔴": 5,
    # SELL — first trim to biggest trim
    "⬆️": 1, "⬆️⬆️": 2, "🚀": 3, "🚀🚀": 4,
    # WATCH
    "👁️": 1,
    # Non-standard signals (SUVEN etc.)
    "⭐": 4, "🚨": 3,
}


@dataclass
class AlertLevel:
    signal: str       # emoji from markdown
    price_str: str    # original string from markdown, e.g. "₹165" or "₹195-200"
    lower: float      # lower bound of trigger range
    upper: float      # upper bound (equals lower for single prices)
    alert_type: str   # "BUY" | "SELL" | "WATCH"
    message: str      # exact alert text to send
    confidence: int   # 1–5 for BUY, 1–4 for SELL (derived from signal emoji)


@dataclass
class CalendarAlert:
    month: int                      # 1-12: fire during this calendar month; 0 for EVENT (never fires)
    year: int                       # calendar year; 0 for EVENT type
    message: str
    alert_type: str = "MONTH"       # "MONTH" | "DATE" | "EVENT"
    exact_date: Optional[date] = None  # set only for DATE type (fires on that exact day)


@dataclass
class StockConfig:
    ticker: str           # e.g. "STLTECH"
    yf_symbol: str        # e.g. "STLTECH.NS"
    name: str
    core_pct: int
    levels: list[AlertLevel] = field(default_factory=list)
    calendar_alerts: list[CalendarAlert] = field(default_factory=list)


@dataclass
class FestivalEvent:
    """One row from the gold festival calendar table."""
    name: str            # e.g. "Akshaya Tritiya"
    event_date: date     # ISO date
    demand_implication: str   # free-form label, e.g. "wedding_season_pickup"


@dataclass
class GoldConfig:
    """
    Parsed gold.md config — sibling to StockConfig but for the commodity tracker.
    Reuses AlertLevel for accumulation zones (₹/gram, 24K) so that engine._crosses()
    can be reused unchanged.
    """
    target_allocation_pct: int
    customs_duty_pct: float           # Indian gold customs duty (e.g. 6.0 for 6%)
    zones: list[AlertLevel] = field(default_factory=list)
    festivals: list[FestivalEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def _parse_price_range(raw: str) -> tuple[float, float]:
    """
    Convert a price cell to (lower, upper).
    Handles: "₹165", "₹195-200", "₹1,100-1,200", "₹500+"
    """
    clean = raw.replace("₹", "").replace(",", "").replace("+", "").strip()
    if "-" in clean:
        lo, hi = clean.split("-", 1)
        return float(lo.strip()), float(hi.strip())
    return float(clean), float(clean)


# ---------------------------------------------------------------------------
# Alert levels table parser
# ---------------------------------------------------------------------------

def _parse_levels(content: str) -> list[AlertLevel]:
    levels = []
    in_table = False

    for line in content.splitlines():
        if "## Alert Levels" in line:
            in_table = True
            continue

        if in_table:
            if line.startswith("##"):
                break  # next section
            if not line.startswith("|"):
                continue

            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) < 4:
                continue

            signal, price_raw, atype, message = cols[0], cols[1], cols[2], cols[3]

            # Skip header and separator rows
            if "Signal" in signal or "---" in price_raw:
                continue

            atype = atype.strip()

            # Only process BUY / SELL / WATCH; skip HOLD and "Always" rows
            if atype not in ("BUY", "SELL", "WATCH") or "Always" in price_raw:
                continue

            try:
                lo, hi = _parse_price_range(price_raw)
            except ValueError:
                continue  # malformed price cell — skip silently

            # Strip surrounding quotes from the alert message
            msg = message.strip().strip('"')

            levels.append(AlertLevel(
                signal=signal,
                price_str=price_raw,
                lower=lo,
                upper=hi,
                alert_type=atype,
                message=msg,
                confidence=SIGNAL_CONFIDENCE.get(signal, 1),
            ))

    return levels


# ---------------------------------------------------------------------------
# Special / calendar alerts parser
# ---------------------------------------------------------------------------

def _parse_calendar_alerts(content: str, ticker: str) -> list[CalendarAlert]:
    """
    Parse the ## Special Alerts table into CalendarAlert objects.

    Expected table format:
        | Date       | Type  | Ticker | Alert Message            |
        |------------|-------|--------|--------------------------|
        | 2026-05-20 | DATE  | STLTECH | "message text"          |
        | 2026-05    | MONTH | BBOX    | "message text"          |
        | EVENT      | EVENT | CGPOWER | "message text"          |

    Date column:
      - YYYY-MM-DD → DATE type  (fires on that exact date)
      - YYYY-MM    → MONTH type (fires on 1st of that month)
      - EVENT      → EVENT type (no date; becomes VTODO in CalDAV)

    EVENT rows have month=0 / year=0 so engine.check_calendar_alerts
    never matches them (it checks ca.month == now.month).
    """
    alerts = []
    in_section = False

    for line in content.splitlines():
        if "## Special Alerts" in line:
            in_section = True
            continue

        if in_section:
            if line.startswith("##"):
                break
            if not line.startswith("|"):
                continue

            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) < 4:
                continue

            date_col, type_col, _ticker_col, msg_col = cols[0], cols[1], cols[2], cols[3]

            # Skip header and separator rows
            if date_col == "Date" or "---" in type_col:
                continue

            atype = type_col.strip().upper()
            msg = msg_col.strip().strip('"')
            if not msg:
                continue

            if atype == "DATE":
                try:
                    d = date.fromisoformat(date_col.strip())
                except ValueError:
                    continue
                alerts.append(CalendarAlert(
                    month=d.month, year=d.year, message=msg,
                    alert_type="DATE", exact_date=d,
                ))

            elif atype == "MONTH":
                try:
                    # Accept "YYYY-MM" or "YYYY-MM-DD" (takes month/year from it)
                    parts = date_col.strip().split("-")
                    yr, mo = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    continue
                alerts.append(CalendarAlert(
                    month=mo, year=yr, message=msg,
                    alert_type="MONTH", exact_date=None,
                ))

            elif atype == "EVENT":
                # Undated watch trigger — never auto-fires; becomes VTODO in CalDAV
                alerts.append(CalendarAlert(
                    month=0, year=0, message=msg,
                    alert_type="EVENT", exact_date=None,
                ))

    return alerts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_stock_file(path: Path) -> Optional[StockConfig]:
    """Parse one stock .md file → StockConfig, or None if it looks invalid."""
    content = path.read_text(encoding="utf-8")

    ticker = path.stem.upper()

    # Name from H1: "# STLTECH — Sterlite Technologies Ltd | NSE: STLTECH"
    h1 = re.search(r"^#\s+\S+\s+[—–]\s+(.+?)(?:\s*\|.*)?$", content, re.MULTILINE)
    name = h1.group(1).strip() if h1 else ticker

    # Core %: "**Core Position:** 50% of invested value"
    core_m = re.search(r"\*\*Core Position:\*\*\s+(\d+)%", content)
    core_pct = int(core_m.group(1)) if core_m else 0

    levels = _parse_levels(content)
    calendar_alerts = _parse_calendar_alerts(content, ticker)

    # A file with no levels isn't a stock config (e.g. template, CLAUDE.md)
    if not levels:
        return None

    return StockConfig(
        ticker=ticker,
        yf_symbol=f"{ticker}.NS",
        name=name,
        core_pct=core_pct,
        levels=levels,
        calendar_alerts=calendar_alerts,
    )


def load_all_stocks(stocks_dir: Path, excluded: set[str]) -> list[StockConfig]:
    """Load all stock configs from .md files in stocks_dir."""
    configs = []
    for md_file in sorted(stocks_dir.glob("*.md")):
        if md_file.name in excluded:
            continue
        cfg = parse_stock_file(md_file)
        if cfg:
            configs.append(cfg)
    return configs


# ---------------------------------------------------------------------------
# Gold config parser (sibling to stock parser)
# ---------------------------------------------------------------------------

def _parse_gold_zones(content: str) -> list[AlertLevel]:
    """
    Parse the ## Accumulation Zones table — same shape as stock alert levels,
    so we reuse AlertLevel and price-range parsing.

    Prices in this table are ₹/gram of 24K gold (NOT GOLDBEES NAV) — the gold
    module converts current GOLDBEES NAV → ₹/gram before calling _crosses().
    """
    levels: list[AlertLevel] = []
    in_table = False

    for line in content.splitlines():
        if "## Accumulation Zones" in line:
            in_table = True
            continue

        if in_table:
            if line.startswith("##"):
                break
            if not line.startswith("|"):
                continue

            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) < 4:
                continue

            signal, price_raw, atype, message = cols[0], cols[1], cols[2], cols[3]

            # Skip header / separator rows
            if "Signal" in signal or "---" in price_raw:
                continue

            atype = atype.strip()
            if atype not in ("BUY", "SELL", "WATCH"):
                continue

            try:
                lo, hi = _parse_price_range(price_raw)
            except ValueError:
                continue

            msg = message.strip().strip('"')

            levels.append(AlertLevel(
                signal=signal,
                price_str=price_raw,
                lower=lo,
                upper=hi,
                alert_type=atype,
                message=msg,
                confidence=SIGNAL_CONFIDENCE.get(signal, 1),
            ))

    return levels


def _parse_gold_festivals(content: str) -> list[FestivalEvent]:
    """Parse all `## Hardcoded Festival Calendar (YYYY)` tables into FestivalEvent rows."""
    events: list[FestivalEvent] = []
    in_table = False

    for line in content.splitlines():
        # Match any "## Hardcoded Festival Calendar" header (one per year)
        if line.lstrip().startswith("## Hardcoded Festival Calendar"):
            in_table = True
            continue

        if in_table:
            if line.startswith("## ") and "Festival Calendar" not in line:
                in_table = False
                continue
            if not line.startswith("|"):
                continue

            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) < 3:
                continue

            festival, date_str, implication = cols[0], cols[1], cols[2]

            if "Festival" in festival or "---" in date_str:
                continue

            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                continue

            events.append(FestivalEvent(
                name=festival,
                event_date=d,
                demand_implication=implication,
            ))

    return events


def parse_gold_file(path: Path) -> Optional[GoldConfig]:
    """Parse commodities/gold.md → GoldConfig."""
    if not path.exists():
        return None

    content = path.read_text(encoding="utf-8")

    # Target allocation: "**Target Allocation:** 8% of total portfolio"
    alloc_m = re.search(r"\*\*Target Allocation:\*\*\s+(\d+)%", content)
    target_pct = int(alloc_m.group(1)) if alloc_m else 0

    # Customs duty inside a fenced ``` block:
    #   INDIAN_GOLD_CUSTOMS_DUTY_PCT: 6
    duty_m = re.search(r"INDIAN_GOLD_CUSTOMS_DUTY_PCT:\s*([\d.]+)", content)
    duty_pct = float(duty_m.group(1)) if duty_m else 6.0  # safe default (post-Jul-2024)

    zones = _parse_gold_zones(content)
    festivals = _parse_gold_festivals(content)

    return GoldConfig(
        target_allocation_pct=target_pct,
        customs_duty_pct=duty_pct,
        zones=zones,
        festivals=festivals,
    )
