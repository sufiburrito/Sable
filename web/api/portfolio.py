"""
Portfolio management endpoints — wraps alert_bot.portfolio for the Web UI.

Three operations:
  POST   /api/portfolio/add               body: {ticker, queue_analysis?: bool}
  DELETE /api/portfolio/{ticker}          archives stocks/{TICKER}.md
  PUT    /api/portfolio/{ticker}/identity body: {sector?: str, core_pct?: int}

All three reuse alert_bot.portfolio so the Web UI and Telegram /portfolio
command share one backend (single source of truth).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from alert_bot import portfolio
from alert_bot.config import STOCKS_DIR

router = APIRouter(prefix="/api/portfolio")


@router.post("/add")
def add(body: dict):
    """
    Add a ticker to the portfolio.

    Body:
      ticker (str, required) — NSE ticker, will be uppercased
      queue_analysis (bool, default True) — whether to queue a full analysis
    """
    ticker = body.get("ticker", "")
    if not ticker:
        raise HTTPException(422, "Missing 'ticker' in request body")

    queue_analysis = bool(body.get("queue_analysis", True))

    result = portfolio.add_ticker(ticker, queue_analysis=queue_analysis)
    if not result.get("ok"):
        # 409 = conflict (already exists), 422 = malformed input, 400 = anything else
        err = result.get("error", "Unknown error")
        if "already in the portfolio" in err:
            raise HTTPException(409, err)
        if "Invalid ticker format" in err:
            raise HTTPException(422, err)
        raise HTTPException(400, err)

    return result


@router.delete("/{ticker}")
def remove(ticker: str):
    """Archive a ticker (full sweep) into archive/{TICKER}/ — recoverable via restore."""
    result = portfolio.remove_ticker(ticker)
    if not result.get("ok"):
        err = result.get("error", "Unknown error")
        if "not in the portfolio" in err:
            raise HTTPException(404, err)
        if "Invalid ticker format" in err:
            raise HTTPException(422, err)
        raise HTTPException(400, err)

    return result


@router.put("/{ticker}/identity")
def update_identity(ticker: str, body: dict):
    """
    Patch the Identity block of stocks/{TICKER}.md in place.

    Body:
      sector (str, optional)
      core_pct (int, optional, 0-100)

    Either or both fields can be sent. If neither is present, returns 422.
    """
    ticker = ticker.upper()
    md_path = STOCKS_DIR / f"{ticker}.md"
    if not md_path.exists():
        raise HTTPException(404, f"{ticker} is not in the portfolio")

    sector = body.get("sector")
    core_pct = body.get("core_pct")

    if sector is None and core_pct is None:
        raise HTTPException(422, "Send at least one of 'sector' or 'core_pct'")

    if core_pct is not None:
        if not isinstance(core_pct, int) or not (0 <= core_pct <= 100):
            raise HTTPException(422, "core_pct must be an int between 0 and 100")

    if sector is not None:
        if not isinstance(sector, str) or not sector.strip():
            raise HTTPException(422, "sector must be a non-empty string")
        sector = sector.strip()

    text = md_path.read_text(encoding="utf-8")
    original = text

    if sector is not None:
        # Replace the **Sector:** line in the Identity block. Match exactly one line.
        text, n = re.subn(
            r"(\*\*Sector:\*\*\s+).+?$",
            lambda m: m.group(1) + sector,
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n == 0:
            raise HTTPException(
                400,
                f"Could not locate **Sector:** line in {ticker}.md — file may be malformed",
            )

    if core_pct is not None:
        # Replace the digit count in **Core Position:** N%
        text, n = re.subn(
            r"(\*\*Core Position:\*\*\s+)\d+(%)",
            lambda m: f"{m.group(1)}{core_pct}{m.group(2)}",
            text,
            count=1,
        )
        if n == 0:
            raise HTTPException(
                400,
                f"Could not locate **Core Position:** N% in {ticker}.md — file may be malformed",
            )

    if text != original:
        md_path.write_text(text, encoding="utf-8")

    sync_result = portfolio.sync_active_stocks_table()

    return {
        "ok": True,
        "ticker": ticker,
        "updated": {
            "sector": sector,
            "core_pct": core_pct,
        },
        "claude_md_synced": sync_result.get("ok", False),
        "claude_md_count": sync_result.get("count"),
    }
