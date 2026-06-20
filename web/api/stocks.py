"""
Stock list and detail endpoints.
Reuses the bot's parser to read stock configs from stocks/*.md.
"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES
from alert_bot.parser import load_all_stocks, parse_stock_file
from alert_bot.portfolio import _read_identity

router = APIRouter(prefix="/api")

# Focus state file — stores which tickers are in the "background" group.
# Everything not listed is foreground (the default).
FOCUS_FILE = Path(__file__).parent.parent.parent / "data" / "focus.json"


def _extract_belief(md_path: Path) -> str | None:
    """Pull the belief level keyword from a stock markdown file.

    Handles two formatting variants found in the stock .md files:
      A) ## Belief Level (date)\\n**WORD** — description...
      B) ## Belief Level — WORD (Strategy A: ...)
    """
    try:
        text = md_path.read_text(encoding="utf-8")
        # Format A: bold keyword on the line after the header
        m = re.search(r"## Belief Level.*?\n\*\*(\w+)\*\*", text, re.DOTALL)
        if m:
            return m.group(1)
        # Format B: keyword inline in the header after an em-dash
        m = re.search(r"## Belief Level\s*—\s*(\w+)", text)
        return m.group(1) if m else None
    except OSError:
        return None


def _load_focus() -> set[str]:
    """Load the set of background tickers from focus.json.

    Returns an empty set if the file doesn't exist or is malformed,
    which means all stocks default to foreground.
    """
    try:
        data = json.loads(FOCUS_FILE.read_text(encoding="utf-8"))
        return set(data.get("background", []))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_focus(background: set[str]) -> None:
    """Write the background ticker set to focus.json."""
    FOCUS_FILE.write_text(
        json.dumps({"background": sorted(background)}, indent=2) + "\n",
        encoding="utf-8",
    )


@router.get("/stocks")
def list_stocks():
    """Return all configured stocks with summary info.

    Includes both fully-parseable stocks (with alert levels) and freshly-added
    stubs that don't yet have levels — the latter are flagged with `parsed: False`
    so the UI can render them differently. This lets users edit/remove a stock
    immediately after adding it without waiting for the first analysis run.
    """
    parsed_stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
    parsed_by_ticker = {s.ticker: s for s in parsed_stocks}
    background = _load_focus()

    result = []
    for md_path in sorted(STOCKS_DIR.glob("*.md")):
        if md_path.name in EXCLUDED_MD_FILES:
            continue
        ticker = md_path.stem.upper()
        sector, core_pct = _read_identity(md_path)
        if sector is None:
            # Doesn't even look like a stock config — skip
            continue
        belief = _extract_belief(md_path)
        s = parsed_by_ticker.get(ticker)
        if s is not None:
            entry = {
                "ticker": s.ticker,
                "name": s.name,
                "core_pct": s.core_pct,
                "sector": sector,
                "level_count": len(s.levels),
                "belief": belief,
                "focus": "background" if s.ticker in background else "foreground",
                "parsed": True,
            }
        else:
            # Stub-only: identity exists but no alert levels yet
            entry = {
                "ticker": ticker,
                "name": ticker,
                "core_pct": core_pct,
                "sector": sector,
                "level_count": 0,
                "belief": None,
                "focus": "background" if ticker in background else "foreground",
                "parsed": False,
            }
        result.append(entry)

    result.sort(key=lambda x: x["ticker"])
    return result


@router.put("/focus/{ticker}")
def set_focus(ticker: str, body: dict):
    """Move a stock to foreground or background.

    Accepts: {"group": "foreground"} or {"group": "background"}
    Idempotent — setting a stock to its current group is a no-op.
    """
    ticker = ticker.upper()
    group = body.get("group", "foreground")

    if group not in ("foreground", "background"):
        raise HTTPException(400, f"Invalid group: {group}. Use 'foreground' or 'background'.")

    background = _load_focus()

    if group == "background":
        background.add(ticker)
    else:
        background.discard(ticker)

    _save_focus(background)
    return {"ok": True, "ticker": ticker, "focus": group}


@router.get("/stocks/{ticker}")
def get_stock(ticker: str):
    """Return full stock config for a single ticker.

    Stubs without alert levels return their Identity block only (so the edit
    modal can still pre-fill sector/core_pct).
    """
    ticker = ticker.upper()
    md_path = STOCKS_DIR / f"{ticker}.md"
    if not md_path.exists():
        raise HTTPException(404, f"Stock {ticker} not found")

    sector_only, core_only = _read_identity(md_path)

    stock = parse_stock_file(md_path)
    if stock is None:
        # Stub fallback — no levels yet
        if sector_only is None:
            raise HTTPException(404, f"Could not parse {ticker}.md (no Identity block)")
        return {
            "ticker": ticker,
            "name": ticker,
            "core_pct": core_only,
            "sector": sector_only,
            "belief": None,
            "levels": [],
            "calendar_alerts": [],
            "parsed": False,
        }

    belief = _extract_belief(md_path)

    levels = []
    for lvl in stock.levels:
        levels.append({
            "signal": lvl.signal,
            "price_str": lvl.price_str,
            "lower": lvl.lower,
            "upper": lvl.upper,
            "alert_type": lvl.alert_type,
            "message": lvl.message,
            "confidence": lvl.confidence,
        })

    calendar = []
    for cal in stock.calendar_alerts:
        calendar.append({
            "message": cal.message,
            "alert_type": cal.alert_type,
        })

    return {
        "ticker": stock.ticker,
        "name": stock.name,
        "core_pct": stock.core_pct,
        "sector": sector_only,
        "belief": belief,
        "levels": levels,
        "calendar_alerts": calendar,
        "parsed": True,
    }
