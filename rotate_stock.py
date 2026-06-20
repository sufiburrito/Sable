#!/usr/bin/env python3
"""
Rotate a stock config file into backups and promote a draft.

Flow:
  1. Read creation date of stocks/TICKER.md (fallback: last-modified date).
  2. Copy stocks/TICKER.md       →  stocks/backups/TICKER_YYYYMMDD.md
  3. Move  nightly-levels/TICKER.md  →  stocks/TICKER.md

Usage:
    python3 rotate_stock.py TICKER

Example:
    python3 rotate_stock.py CGPOWER
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

STOCKS_DIR   = Path(__file__).parent / "stocks"
BACKUPS_DIR  = STOCKS_DIR / "backups"
DRAFTS_DIR   = Path(__file__).parent / "nightly-levels"


def _file_date(path: Path) -> str:
    """
    Return YYYYMMDD string for the file's creation date.
    Uses st_birthtime (macOS / BSD) if available; falls back to st_mtime.
    """
    stat = path.stat()
    ts = getattr(stat, "st_birthtime", None) or stat.st_mtime
    return datetime.fromtimestamp(ts).strftime("%Y%m%d")


def rotate(ticker: str) -> None:
    ticker = ticker.upper()
    current = STOCKS_DIR / f"{ticker}.md"
    draft   = DRAFTS_DIR / f"{ticker}.md"

    if not current.exists():
        raise FileNotFoundError(f"No existing stock file: {current}")
    if not draft.exists():
        raise FileNotFoundError(f"No draft to promote: {draft}")

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    date_str = _file_date(current)
    backup   = BACKUPS_DIR / f"{ticker}_{date_str}.md"

    # If a backup for this date already exists, append a counter
    if backup.exists():
        counter = 1
        while backup.exists():
            backup = BACKUPS_DIR / f"{ticker}_{date_str}_{counter}.md"
            counter += 1

    shutil.copy2(str(current), str(backup))
    print(f"Backed up:  {current.name}  →  backups/{backup.name}")

    shutil.move(str(draft), str(current))
    print(f"Promoted:   nightly-levels/{draft.name}  →  stocks/{current.name}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 rotate_stock.py TICKER")
        sys.exit(1)
    try:
        rotate(sys.argv[1])
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
