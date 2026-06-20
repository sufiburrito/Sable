"""
CalDAV calendar sync via Radicale.

Radicale is a lightweight CalDAV/CardDAV server written in Python.
We run it as a daemon thread inside the bot process and write .ics files
directly to its storage directory — no HTTP client library needed.

Calendar subscription URL (replace <tailscale-ip> with your Tailscale IP):
  http://<tailscale-ip>:5232/algotrading-events/

Event graduation:
  EVENT rows in stocks/*.md start as VTODO items (undated tasks in Apple Reminders
  / Google Tasks).  When a future analysis run discovers a real date for one of them,
  Claude updates the row: EVENT|EVENT → YYYY-MM-DD|DATE (same message text).
  Because the UID is keyed on ticker+message (not date), the same .ics filename is
  overwritten — the VTODO is replaced by a VEVENT and the item moves from Reminders
  into the calendar grid automatically.  No manual cleanup needed.
"""
import hashlib
import json
import logging
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from icalendar import Calendar, Event, Todo, vText

from .config import CALDAV_CALENDAR_NAME, CALDAV_USER
from .parser import CalendarAlert, StockConfig

logger = logging.getLogger(__name__)

# Stable namespace UUID for this project's UIDs.
# All event UIDs are derived from this so they are deterministic across restarts.
_PROJECT_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "algotrading.caldav")


# ---------------------------------------------------------------------------
# UID helpers
# ---------------------------------------------------------------------------

def _make_uid(ticker: str, message: str) -> str:
    """
    Deterministic UUID keyed on ticker + message text.

    Keying on message (not date) means:
      - Same message, date changed → same UID → .ics overwritten in place
      - Message text changed → new UID → old file deleted, new one written
      - EVENT row promoted to DATE (same message) → VTODO overwritten with VEVENT
    """
    msg_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
    return str(uuid.uuid5(_PROJECT_NS, f"{ticker}:{msg_hash}"))


# ---------------------------------------------------------------------------
# RFC 5545 builders
# ---------------------------------------------------------------------------

def _build_vevent(uid: str, ticker: str, ca: CalendarAlert) -> bytes:
    """
    Build a VEVENT .ics for DATE and MONTH alerts.
    Produces an all-day event anchored to the alert date.
    """
    cal = Calendar()
    cal.add("prodid", "-//algotrading//stockbot//EN")
    cal.add("version", "2.0")
    # CALSCALE and METHOD are required by some clients (Apple Calendar, Google)
    cal.add("calscale", "GREGORIAN")

    event = Event()
    event.add("uid", vText(uid))
    event.add("summary", vText(ca.message))
    event.add("categories", vText(ticker))

    # Determine the anchor date
    if ca.alert_type == "DATE" and ca.exact_date:
        anchor = ca.exact_date
    else:
        # MONTH: fire on the 1st of that month
        anchor = date(ca.year, ca.month, 1)

    # DTSTART as a DATE value (all-day event — no time component)
    event.add("dtstart", anchor)
    # DTEND is the day after for all-day events per RFC 5545
    event.add("dtend", anchor + timedelta(days=1))

    # DTSTAMP is required — use UTC now
    event.add("dtstamp", datetime.now(tz=timezone.utc))
    event.add("description", vText(f"Alert type: {ca.alert_type} | Ticker: {ticker}"))

    cal.add_component(event)
    return cal.to_ical()


def _build_vtodo(uid: str, ticker: str, ca: CalendarAlert) -> bytes:
    """
    Build a VTODO .ics for EVENT alerts (undated watch triggers).
    VTODO items appear in Apple Reminders / Google Tasks, not in the calendar grid.
    This is the semantically correct RFC 5545 representation for an undated trigger.
    """
    cal = Calendar()
    cal.add("prodid", "-//algotrading//stockbot//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    todo = Todo()
    todo.add("uid", vText(uid))
    todo.add("summary", vText(ca.message))
    todo.add("categories", vText(ticker))
    todo.add("dtstamp", datetime.now(tz=timezone.utc))
    todo.add("description", vText(f"Undated watch trigger | Ticker: {ticker}"))
    # STATUS:NEEDS-ACTION marks it as an open reminder (not completed)
    todo.add("status", vText("NEEDS-ACTION"))

    cal.add_component(todo)
    return cal.to_ical()


# ---------------------------------------------------------------------------
# Collection setup
# ---------------------------------------------------------------------------

def _ensure_collection(storage_dir: Path, calendar_name: str) -> Path:
    """
    Create the Radicale collection directory and its .Radicale.props metadata
    file if they don't already exist.

    Radicale's filesystem storage layout:
      data/calendar/
        collection-root/
          stock/                    ← user directory (matches CALDAV_USER)
            algotrading-events/
              .Radicale.props   ← calendar metadata (JSON)
              <uid>.ics         ← one file per event
    """
    # Collection must live under /{user}/ for Radicale rights to grant access
    collection_dir = storage_dir / "collection-root" / CALDAV_USER / calendar_name
    collection_dir.mkdir(parents=True, exist_ok=True)

    props_file = collection_dir / ".Radicale.props"
    if not props_file.exists():
        props = {
            "tag": "VCALENDAR",
            "D:displayname": "Stock Alerts",
            "C:calendar-description": "Upcoming stock events and earnings dates",
            "ICAL:calendar-color": "#2196F3FF",  # blue
        }
        props_file.write_text(json.dumps(props), encoding="utf-8")
        logger.debug("CalDAV: created collection at %s", collection_dir)

    return collection_dir


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_calendar(stocks: list[StockConfig], storage_dir: Path) -> int:
    """
    Full reconcile: compute expected UIDs from current stock configs, then:
      1. Delete .ics files whose UIDs are no longer in the config (stale events)
      2. Write/overwrite .ics for all current alerts

    Returns the number of .ics files written.

    This is intentionally a full replace-in-place approach:
      - Same UID → file overwritten (date change, or EVENT→DATE graduation)
      - Missing UID → old file deleted
      - New UID → new file created
    """
    collection_dir = _ensure_collection(storage_dir, CALDAV_CALENDAR_NAME)

    # Build the full set of (uid → ics_bytes) we expect to exist
    expected: dict[str, bytes] = {}
    for stock in stocks:
        for ca in stock.calendar_alerts:
            uid = _make_uid(stock.ticker, ca.message)
            if ca.alert_type in ("DATE", "MONTH"):
                ics = _build_vevent(uid, stock.ticker, ca)
            else:
                # EVENT → VTODO
                ics = _build_vtodo(uid, stock.ticker, ca)
            expected[uid] = ics

    # Delete stale .ics files (UIDs no longer in the config)
    for ics_file in collection_dir.glob("*.ics"):
        if ics_file.stem not in expected:
            ics_file.unlink()
            logger.debug("CalDAV: deleted stale event %s", ics_file.name)

    # Write all current events (overwrite if already exists — idempotent)
    for uid, ics_bytes in expected.items():
        (collection_dir / f"{uid}.ics").write_bytes(ics_bytes)

    logger.info("CalDAV: synced %d events to %s", len(expected), collection_dir)
    return len(expected)


def start_radicale(ini_path: Path) -> threading.Thread:
    """
    Start Radicale as a daemon thread using its embedded server API.

    Daemon thread means it shuts down automatically when the main process exits —
    no cleanup needed.  Radicale serves CalDAV on 127.0.0.1:5232 (see radicale.ini).
    """
    import radicale
    import radicale.app
    import radicale.config
    import radicale.server

    def _run() -> None:
        try:
            # Load our ini file — config.load expects (str, bool) tuples
            config = radicale.config.load([(str(ini_path), True)])
            # serve() creates the Application internally; do not pass it as an arg
            radicale.server.serve(config)
        except Exception:
            logger.exception("CalDAV: Radicale server crashed")

    t = threading.Thread(target=_run, name="radicale", daemon=True)
    t.start()
    logger.info("CalDAV: Radicale started (port %s)", ini_path)
    return t
