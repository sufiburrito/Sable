"""
Dalal Street Morning Digest — utility functions for the autonomous loop.

The actual intelligence (reasoning about macro → portfolio connections) is
handled by Claude in the autonomous loop (LOOP_PROMPT.md).  This module
provides:
  - build_sector_lookup() — scans stocks/*.md at startup, writes a compact
    data/stock_sectors.json for Claude to read during digest processing
  - check_new_digests() — returns list of unprocessed YYYY-MM-DD date strings
  - mark_digest_processed() — records a date so it's never re-sent

Files are named YYYY-MM-DD.md in dalalstreet_morning/.
"""
import json
import logging
import re
from pathlib import Path

from .state import BotState

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")
_SECTORS_FILE = Path(__file__).parent.parent / "data" / "stock_sectors.json"


# ---------------------------------------------------------------------------
# Sector lookup — built once at startup, read by Claude in the loop
# ---------------------------------------------------------------------------

def build_sector_lookup(stocks_dir: Path) -> dict[str, str]:
    """
    Scan all stock .md files and extract the Sector line from ## Identity.
    Returns {TICKER: "sector string"}.  Writes to data/stock_sectors.json
    so the autonomous loop can read it without parsing markdown.
    """
    sectors: dict[str, str] = {}
    for md_file in sorted(stocks_dir.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        ticker = md_file.stem.upper()
        try:
            content = md_file.read_text(encoding="utf-8")
            m = re.search(r"\*\*Sector:\*\*\s*(.+)", content)
            if m:
                sectors[ticker] = m.group(1).strip()
        except OSError:
            pass

    _SECTORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        _SECTORS_FILE.write_text(json.dumps(sectors, indent=2), encoding="utf-8")
        logger.info(f"Built sector lookup: {len(sectors)} stocks → {_SECTORS_FILE}")
    except OSError as e:
        logger.warning(f"Could not write sector lookup: {e}")

    return sectors


# ---------------------------------------------------------------------------
# File detection — used by the Python bot loop to track processed dates
# ---------------------------------------------------------------------------

def check_new_digests(digest_dir: Path, state: BotState) -> list[str]:
    """
    Return list of date strings (YYYY-MM-DD) for new, unprocessed digest files.
    """
    if not digest_dir.exists():
        return []

    new_dates = []
    for md_file in sorted(digest_dir.glob("*.md")):
        m = _DATE_RE.search(md_file.name)
        if not m:
            continue
        date_str = m.group(1)
        if not state.is_digest_processed(date_str):
            new_dates.append(date_str)

    return new_dates
