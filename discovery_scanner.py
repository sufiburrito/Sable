"""
Multibagger discovery scanner.

Joins four data sources into a conviction score per candidate:
  1. Smart money flow (insider_activity.json → explore_candidates)
  2. Macro alignment (macro_signals.json → sector direction + causal chain)
  3. Technical setup (OHLC → Weinstein stage, RSI, relative strength vs Nifty)
  4. News signals (news_signals.json → ticker-level news with causal chains)

No new data collection — everything from local files already produced by
other parts of the system.  No Claude calls — pure Python scoring.

Weekly output: data/discovery_watchlist.json + Telegram digest.

Usage:
    # Test scan
    python3 discovery_scanner.py

    # Called from alert_bot/main.py on Sundays (same pattern as gold digest)
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from alert_bot.ohlc_cache import load_ohlc_cached

# Reuse the technical analysis functions already built for confidence scoring.
# These are module-private (_prefix) but Python doesn't enforce that — we
# import them directly to avoid duplicating Weinstein/RSI/Nifty logic.
from alert_bot.confidence import _weinstein_stage, _compute_rsi, _load_nifty

logger = logging.getLogger(__name__)

_DATA_DIR = Path("data")
_ANALYSIS_DIR = Path("analysis")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict | None:
    """Load a JSON file, return None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _portfolio_tickers() -> list[str]:
    """Return tickers currently held in portfolio.db (to exclude from discovery)."""
    db_path = _DATA_DIR / "portfolio.db"
    if not db_path.exists():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM transactions WHERE symbol IS NOT NULL"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _build_sector_map() -> dict[str, str]:
    """
    Build ticker → sector mapping.
    Primary: stock_universe.json (curated ~130 tickers).
    Supplement: sector_signals.json (derived from recent insider trade data —
    covers any ticker that appeared in bulk/insider CSVs, not just the universe).
    """
    mapping: dict[str, str] = {}

    universe = _load_json(_DATA_DIR / "stock_universe.json")
    if universe:
        for theme_name, theme_data in universe.items():
            if theme_name.startswith("_"):
                continue
            for tier_key in ("tier1", "tier2"):
                for stock in theme_data.get(tier_key, []):
                    ticker = stock.get("ticker", "")
                    if ticker:
                        mapping[ticker] = theme_name

    # sector_signals.json covers tickers from recent insider trade CSVs that
    # aren't in the curated universe — keeps sector labels fresh automatically.
    sector_sigs = _load_json(_DATA_DIR / "sector_signals.json")
    if sector_sigs:
        for sector, data in sector_sigs.get("sectors", {}).items():
            for stock in data.get("top_stocks", []):
                ticker = stock.get("ticker", "")
                if ticker and ticker not in mapping:
                    mapping[ticker] = sector

    return mapping


def _build_entity_counts(insider_data: dict) -> dict[str, int]:
    """Map ticker → number of coordinated buying entities."""
    counts: dict[str, int] = {}
    for coord in insider_data.get("coordinated_buys", []):
        ticker = coord.get("ticker", "")
        if ticker:
            counts[ticker] = len(coord.get("parties", []))
    return counts


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def _score_smart_money(candidate: dict, entity_counts: dict) -> int:
    """
    Score 0-3 based on the type and breadth of smart money signal.
    Promoter buying = strongest signal (they know the company best).
    Coordinated institutional with many entities = broad consensus.
    """
    reason = candidate.get("reason", "")
    ticker = candidate.get("ticker", "")

    if reason == "promoter_accumulation":
        return 3

    # Coordinated institutional — score by breadth
    entities = entity_counts.get(ticker, 0)
    if entities >= 5:
        return 2
    return 1


def _score_capital(candidate: dict) -> int:
    """
    Score 0-2 based on how much capital is committed.
    Big money = big conviction from whoever is buying.
    """
    value = candidate.get("value_cr", 0)
    if value > 500:
        return 2
    if value > 100:
        return 1
    return 0


def _score_macro(sector: str, macro_signals: dict | None) -> tuple[int, str]:
    """
    Score 0-2 based on whether the candidate's sector has a macro tailwind.
    Returns (score, reason_text).

    Primary source: signals[] — sector-keyed structured entries written by LOOP_PROMPT.
    Fallback: macro_themes[] — flat thematic list; keyword-matches sector name against
    theme string. Capped at score 1 (no strength field available).
    """
    if not macro_signals:
        return 0, ""

    # Primary: structured signals[] with per-sector direction + strength
    for signal in macro_signals.get("signals", []):
        if signal.get("sector", "") == sector:
            direction = signal.get("direction", "")
            strength  = signal.get("strength", "")
            reason    = signal.get("reason", "")
            if direction == "tailwind":
                return (2 if strength == "strong" else 1), reason
            elif direction == "headwind":
                # Headwind gives zero — smart money may know something macro doesn't.
                return 0, reason

    # Fallback: macro_themes[] — keyword match sector name in theme identifier
    for theme in macro_signals.get("macro_themes", []):
        theme_name = theme.get("theme", "").lower()
        if sector.lower() in theme_name or any(
            word in theme_name for word in sector.lower().split()
        ):
            direction = theme.get("direction", "")
            note      = theme.get("note", "")
            if direction == "tailwind":
                return 1, note   # capped at 1 — no strength field in macro_themes
            elif direction == "headwind":
                return 0, note

    return 0, ""


def _score_technical(ticker: str) -> dict:
    """
    Score 0-3 based on Weinstein stage analysis.
    Also returns RSI, relative strength, current price, and stage
    description for display in the digest.
    """
    result = {
        "score": 0,
        "stage": 0,
        "stage_desc": "No data",
        "rsi": None,
        "rs_vs_nifty": None,
        "current_price": None,
    }

    # Fetch OHLC — 2 years is enough for Weinstein (needs 150 trading days)
    try:
        df = load_ohlc_cached(ticker, f"{ticker}.NS", "2y")
    except Exception:
        return result

    if df is None or df.empty or len(df) < 30:
        return result

    result["current_price"] = round(float(df["Close"].iloc[-1]), 2)

    # Weinstein stage
    if len(df) >= 150:
        stage, desc = _weinstein_stage(df)
        result["stage"] = stage
        result["stage_desc"] = desc

        if stage == 2 and "advancing" in desc:
            result["score"] = 3
        elif stage == 1:
            result["score"] = 2
        elif stage == 2 and "pullback" in desc:
            result["score"] = 1
        # Stage 3/4 = 0

    # RSI
    if len(df) >= 20:
        result["rsi"] = round(_compute_rsi(df["Close"].values))

    # Relative strength vs Nifty (63 trading days = ~3 months)
    nifty = _load_nifty()
    if nifty is not None and len(df) >= 63 and len(nifty) >= 63:
        stock_ret = (df["Close"].iloc[-1] / df["Close"].iloc[-63] - 1) * 100
        nifty_ret = (nifty["Close"].iloc[-1] / nifty["Close"].iloc[-63] - 1) * 100
        result["rs_vs_nifty"] = round(float(stock_ret - nifty_ret), 1)

    return result


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def _score_news(ticker: str, news_signals: dict | None) -> tuple[int, str]:
    """
    Score 0-1 based on whether a news signal mentions this specific ticker.

    Returns (score, headline_text).
    A ticker-level news hit is more specific than a sector-level macro signal —
    it means the market is talking about THIS stock right now.
    Only counts signals with a causal chain (templated or Claude-reviewed).
    Unreviewed signals don't score — they might be noise.
    """
    if not news_signals:
        return 0, ""

    for sig in news_signals.get("signals", {}).values():
        if sig.get("needs_review"):
            continue  # unreviewed = might be noise, don't score
        tickers = sig.get("affected_tickers", [])
        if ticker in tickers:
            headline = sig.get("headline", "")
            source = sig.get("source", "")
            date = sig.get("date", "")
            # Compact reference: "headline (Source, Apr 25)"
            date_short = ""
            if date:
                try:
                    dt = datetime.strptime(date, "%Y-%m-%d")
                    date_short = dt.strftime("%b %-d")
                except ValueError:
                    date_short = date
            ref = f"{headline[:60]}"
            if source or date_short:
                ref += f" ({source}, {date_short})" if source and date_short else f" ({source or date_short})"
            return 1, ref

    return 0, ""


def scan() -> list[dict]:
    """
    Score all explore_candidates and return a ranked list.

    Each candidate gets a conviction score (0-11) from 5 dimensions:
      smart_money (0-3) + capital (0-2) + macro (0-2) + technical (0-3) + news (0-1)

    Returns list of dicts sorted by conviction descending.
    Also writes data/discovery_watchlist.json.
    """
    # Primary: query market.db for recent smart money buy activity
    candidates = []
    entity_counts: dict[str, int] = {}
    try:
        import market_db as _mdb
        _conn = _mdb.get_conn()
        candidates    = _mdb.query_explore_candidates(
            _conn, days=30, min_value_cr=5,
            portfolio_tickers=_portfolio_tickers(),
        )
        entity_counts = _mdb.query_entity_counts(_conn, days=30)
        _conn.close()
        logger.info(f"Discovery: {len(candidates)} candidates from market.db")
    except Exception as _e:
        logger.warning(f"Discovery: market.db unavailable ({_e}), falling back to JSON")
        insider = _load_json(_DATA_DIR / "insider_activity.json")
        if not insider:
            logger.info("Discovery: no insider_activity.json either, skipping")
            return []
        candidates    = insider.get("explore_candidates", [])
        entity_counts = _build_entity_counts(insider)

    if not candidates:
        logger.info("Discovery: no explore_candidates, skipping")
        return []

    macro_signals = _load_json(_DATA_DIR / "macro_signals.json")
    news_signals  = _load_json(_DATA_DIR / "news_signals.json")
    sector_map    = _build_sector_map()

    # Archived tickers were deliberately retired — never re-surface them in discovery.
    try:
        from alert_bot.portfolio import load_archived_set
        archived = load_archived_set()
    except Exception:
        archived = set()

    results = []
    for c in candidates:
        ticker = c.get("ticker", "")
        if not ticker:
            continue
        if ticker.upper() in archived:
            continue

        sector = sector_map.get(ticker, "Unknown")

        # Score each dimension
        sm_score = _score_smart_money(c, entity_counts)
        cap_score = _score_capital(c)
        macro_score, macro_reason = _score_macro(sector, macro_signals)
        tech = _score_technical(ticker)
        news_score, news_ref = _score_news(ticker, news_signals)

        conviction = sm_score + cap_score + macro_score + tech["score"] + news_score

        # Determine tier (adjusted for 0-11 range with news dimension)
        if conviction >= 7:
            tier = "HIGH CONVICTION"
        elif conviction >= 4:
            tier = "BUILDING"
        else:
            tier = "EARLY SIGNAL"

        results.append({
            "ticker": ticker,
            "name": c.get("stock_name", ticker),
            "sector": sector,
            "conviction": conviction,
            "tier": tier,
            "scores": {
                "smart_money": sm_score,
                "capital": cap_score,
                "macro": macro_score,
                "technical": tech["score"],
                "news": news_score,
            },
            "smart_money_narrative": c.get("narrative", ""),
            "value_cr": c.get("value_cr", 0),
            "reason": c.get("reason", ""),
            "macro_reason": macro_reason,
            "news_ref": news_ref,
            "technical": {
                "stage": tech["stage"],
                "stage_desc": tech["stage_desc"],
                "rsi": tech["rsi"],
                "rs_vs_nifty": tech["rs_vs_nifty"],
                "current_price": tech["current_price"],
            },
        })

    # Sort by conviction descending, then by value_cr descending (tiebreaker)
    results.sort(key=lambda x: (x["conviction"], x["value_cr"]), reverse=True)

    # Persist watchlist
    watchlist = {
        "scan_date": datetime.now().strftime("%Y-%m-%d"),
        "candidates": results,
    }
    watchlist_path = _DATA_DIR / "discovery_watchlist.json"
    watchlist_path.write_text(
        json.dumps(watchlist, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"Discovery: watchlist written ({len(results)} candidates)")

    return results


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------

def format_telegram_digest(candidates: list[dict]) -> str:
    """
    Format the discovery watchlist as a Telegram HTML message.

    Groups candidates by conviction tier: HIGH CONVICTION, BUILDING, EARLY SIGNAL.
    Each candidate shows: smart money narrative, technical setup, macro context.
    """
    now = datetime.now()
    lines = [
        f"<b>🔍  DISCOVERY WATCHLIST</b>",
        f"{now.strftime('%A, %B %-d, %Y')}",
        "",
    ]

    # Group by tier (preserve sort order within each tier)
    tier_emoji = {
        "HIGH CONVICTION": "🏆",
        "BUILDING": "⚡",
        "EARLY SIGNAL": "📡",
    }
    tier_order = ["HIGH CONVICTION", "BUILDING", "EARLY SIGNAL"]

    for tier in tier_order:
        group = [c for c in candidates if c["tier"] == tier]
        if not group:
            continue

        lines.append(f"<b>{tier_emoji[tier]}  {tier}</b>")
        lines.append("")

        # HIGH CONVICTION and BUILDING: full detail
        # EARLY SIGNAL: compact one-liner to save space
        if tier == "EARLY SIGNAL":
            for c in group:
                ticker = c["ticker"]
                conv = c["conviction"]
                sector = c["sector"]
                value = c["value_cr"]
                lines.append(
                    f"{ticker} ({conv}/11) — {sector} · "
                    f"₹{value:,.0f} Cr · "
                    f"<i>technicals say wait</i>"
                )
            lines.append("")
            continue

        for c in group:
            ticker = c["ticker"]
            conv = c["conviction"]
            sector = c["sector"]
            tech = c["technical"]

            # Header: TICKER (score) — sector
            lines.append(f"<b>{ticker}</b> ({conv}/11) — {sector}")

            # Smart money line
            lines.append(c["smart_money_narrative"])

            # Technical line (only if we have data)
            tech_parts = []
            if tech["stage"] > 0:
                tech_parts.append(tech["stage_desc"])
            if tech["rs_vs_nifty"] is not None:
                sign = "+" if tech["rs_vs_nifty"] > 0 else ""
                tech_parts.append(f"{sign}{tech['rs_vs_nifty']:.0f}% vs Nifty")
            if tech["rsi"] is not None:
                tech_parts.append(f"RSI {tech['rsi']}")
            if tech_parts:
                lines.append(" · ".join(tech_parts))

            # Macro context (from macro_signals.json reason field)
            if c["macro_reason"]:
                lines.append(f"Macro: {c['macro_reason']}")

            # News context (from news_signals.json — ticker-level news hit)
            if c.get("news_ref"):
                lines.append(f"News: {c['news_ref']}")

            # Current price
            if tech["current_price"]:
                lines.append(f"Current ₹{tech['current_price']:,.0f}")

            # Guidance for BUILDING tier
            if tier == "BUILDING" and tech["stage"] == 1:
                lines.append("<i>Watch for Stage 2 breakout confirmation</i>")

            lines.append("")

    # Trim trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    body = "\n".join(lines)

    # Telegram message limit is 4096 chars — truncate if needed
    if len(body) > 4000:
        body = body[:3990] + "\n…"

    return body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    candidates = scan()
    if not candidates:
        print("No candidates found.")
    else:
        print(f"\n{len(candidates)} candidates scored.\n")
        print(format_telegram_digest(candidates))
        print(f"\nWatchlist written to data/discovery_watchlist.json")
