"""
Discovery Matrix API endpoint.

Serves GET /api/discovery — merges data/discovery_watchlist.json with
market.db entity counts + recency, plus party_type enrichment from
data/insider_profiles.json.

party_type is derived from insider_profiles.json (keyed by party name):
  - Any party with a promoter category → "promoter"
  - Any party with confidence = "very_high" → "promoter"
  - Any party with confidence = "high" → "institution"
  - Otherwise → "entity"
  Takes the highest-priority type among all parties that traded the ticker.
"""
import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api")

_DATA_DIR = Path("data")

_PROMOTER_CATS = {
    "Insider - Promoter",
    "Insider - Promoter Group",
    "Insider - Promoter & Director",
}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _party_type_for_ticker(ticker: str, profiles: dict) -> str:
    """
    Scan all party profiles for those that traded this ticker.
    Returns the strongest signal type: promoter > institution > entity.
    """
    best = "entity"
    for party_data in profiles.values():
        if ticker not in party_data.get("stocks_traded", []):
            continue
        cat  = party_data.get("category", "")
        conf = party_data.get("confidence", "")
        if cat in _PROMOTER_CATS or conf == "very_high":
            return "promoter"   # highest priority — no need to scan further
        if conf == "high" and best == "entity":
            best = "institution"
    return best


def _days_ago_for_ticker(ticker: str, conn) -> int:
    """Return days since most recent insider trade for this ticker."""
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM insider_trades WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row and row[0]:
            return (date.today() - date.fromisoformat(row[0])).days
    except Exception:
        pass
    return 90   # fallback: treat as stale


@router.get("/discovery")
def get_discovery():
    """
    Return enriched discovery watchlist for the Discovery Matrix chart.

    Merges conviction scores from discovery_watchlist.json with:
    - entity_count from market.db (distinct buyers in last 30 days)
    - days_ago from market.db (recency of most recent trade)
    - party_type from insider_profiles.json (promoter / institution / entity)

    market.db failures are silent — entity_count falls back to 1,
    days_ago falls back to 90, party_type falls back to "entity".
    """
    watchlist = _load_json(_DATA_DIR / "discovery_watchlist.json")
    if not watchlist:
        return {"scan_date": None, "candidates": []}

    profiles = _load_json(_DATA_DIR / "insider_profiles.json") or {}

    # Try to open market.db for entity counts + recency
    entity_counts: dict[str, int] = {}
    conn = None
    try:
        import market_db as mdb
        conn = mdb.get_conn()
        entity_counts = mdb.query_entity_counts(conn, days=30)
    except Exception:
        pass   # non-critical — watchlist entity_count is a reasonable fallback

    results = []
    for c in watchlist.get("candidates", []):
        ticker = c.get("ticker", "")
        if not ticker:
            continue

        tech = c.get("technical", {})

        candidate = {
            "ticker":        ticker,
            "name":          c.get("name", ticker),
            "sector":        c.get("sector", "Unknown"),
            "value_cr":      c.get("value_cr", 0),
            "entity_count":  entity_counts.get(ticker, 1),
            "conviction":    c.get("conviction", 0),
            "tier":          c.get("tier", "EARLY SIGNAL"),
            "days_ago":      _days_ago_for_ticker(ticker, conn) if conn else 90,
            "stage":         tech.get("stage", 0),
            "stage_desc":    tech.get("stage_desc", "No data"),
            "rsi":           tech.get("rsi"),
            "rs_vs_nifty":   tech.get("rs_vs_nifty"),
            "current_price": tech.get("current_price"),
            "narrative":     c.get("smart_money_narrative", ""),
            "macro_reason":  c.get("macro_reason", ""),
            "news_ref":      c.get("news_ref", ""),
            "reason":        c.get("reason", ""),
            "scores":        c.get("scores", {}),
            "party_type":    _party_type_for_ticker(ticker, profiles),
        }
        results.append(candidate)

    if conn:
        conn.close()

    return {
        "scan_date":  watchlist.get("scan_date"),
        "candidates": results,
    }
