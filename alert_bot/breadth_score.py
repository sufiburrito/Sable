# alert_bot/breadth_score.py
"""
5-component market breadth health score.
Per docs/market_breadth_methodology.md.

Components (weights):
  A/D ratio             25%
  % above 200-DMA       25%
  New highs/lows        20%
  Sector participation  15%
  Divergence            15%

refresh() has network calls — runs nightly, never in hot path.
Pure component functions are hot-path safe (used by confidence.py Factor 12).
Writes data/breadth.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

BREADTH_PATH  = Path("data/breadth.json")
UNIVERSE_PATH = Path("data/stock_universe.json")

# NSE sector index symbols for sector participation scoring
SECTOR_INDICES = {
    "BANK":   "^NSEBANK",  "IT":     "^CNXIT",
    "PHARMA": "^CNXPHARMA","FMCG":   "^CNXFMCG",
    "ENERGY": "^CNXENERGY","REALTY":  "^CNXREALTY",
    "METAL":  "^CNXMETAL", "AUTO":    "^CNXAUTO",
}

# Exposure recommendation per zone — gives actionable sizing guidance
_EXPOSURE_MAP = {
    "STRONG":    "85-100% deployed",
    "HEALTHY":   "75-85% deployed",
    "NEUTRAL":   "60-75% deployed",
    "WEAKENING": "40-60% deployed — build cash",
    "CRITICAL":  "<40% deployed — capital preservation",
}


def _zone_for(score: float) -> str:
    """
    Map composite score to zone label.
    Uses >= chain (not a dict/tuple-range) to avoid boundary bug at score=100.0.
    With a `lo <= score < hi` range dict, score=100.0 would NOT match (80, 100)
    because 100 < 100 is False. The >= chain handles all edge cases correctly.
    """
    if score >= 80:   return "STRONG"
    elif score >= 60: return "HEALTHY"
    elif score >= 40: return "NEUTRAL"
    elif score >= 20: return "WEAKENING"
    else:             return "CRITICAL"


def score_ad_ratio(advancing: int, declining: int) -> float:
    """
    Score based on advancing/declining stock ratio.
    Weight: 25% of composite.

    A/D ratio above 0.70 (7 advancers per 3 decliners) is broad bull strength.
    Equal split (0.50) maps to neutral 50.
    """
    total = advancing + declining
    if total == 0:
        return 50.0  # no data — neutral
    ratio = advancing / total
    if ratio >= 0.70:   return 90
    elif ratio >= 0.60: return 75
    elif ratio >= 0.55: return 62
    elif ratio >= 0.45: return 50
    elif ratio >= 0.40: return 35
    elif ratio >= 0.30: return 20
    return 10


def score_pct_above_200dma(pct: float) -> float:
    """
    Score based on percentage of stocks trading above their 200-day moving average.
    Weight: 25% of composite. pct is a fraction (0.0–1.0).

    Above 75% — broad participation; below 25% — majority stocks in downtrend.
    """
    if pct >= 0.75:   return 90
    elif pct >= 0.65: return 75
    elif pct >= 0.55: return 62
    elif pct >= 0.45: return 50
    elif pct >= 0.35: return 35
    elif pct >= 0.25: return 20
    return 10


def score_new_highs_lows(highs: int, lows: int) -> float:
    """
    Score based on the ratio of 52-week new highs to new highs+lows.
    Weight: 20% of composite.

    Dominance of new highs signals expanding leadership; dominance of new lows
    signals broad distribution.
    """
    total = highs + lows
    if total == 0:
        return 50.0  # no data — neutral
    ratio = highs / total
    if ratio >= 0.80:   return 90
    elif ratio >= 0.65: return 75
    elif ratio >= 0.55: return 60
    elif ratio >= 0.45: return 50
    elif ratio >= 0.30: return 30
    return 15


def score_sector_participation(green_count: int, total_count: int) -> float:
    """
    Score based on how many of the tracked sector indices closed green.
    Weight: 15% of composite.

    Broad sector participation confirms a move is genuine, not carried by one sector.
    """
    if total_count == 0:
        return 50.0  # no sectors tracked — neutral
    ratio = green_count / total_count
    if ratio >= 0.80:   return 90
    elif ratio >= 0.65: return 75
    elif ratio >= 0.50: return 58
    elif ratio >= 0.35: return 40
    return 20


def score_divergence(nifty_positive: bool, breadth_positive: bool) -> float:
    """
    Score based on agreement (or divergence) between index direction and breadth.
    Weight: 15% of composite.

    Confirmed bull (both up) = 80, confirmed bear (both down) = 20.
    Bearish divergence (index up, breadth weak) = 30 — most dangerous scenario.
    Bullish divergence (index down, breadth strong) = 70 — potential reversal signal.
    """
    if nifty_positive and breadth_positive:             return 80  # confirmed bull
    elif not nifty_positive and not breadth_positive:   return 20  # confirmed bear
    elif nifty_positive and not breadth_positive:       return 30  # bearish divergence — warning
    else:                                               return 70  # bullish divergence — recovery signal


def compute_composite(
    ad_score: float,
    pct_dma_score: float,
    highs_lows_score: float,
    sector_score: float,
    divergence_score: float,
) -> dict:
    """
    Blend 5 component scores into a weighted composite (0–100).
    Returns dict with composite_score, zone, and exposure_recommendation.

    Weights: A/D 25%, %above200 25%, highs/lows 20%, sector 15%, divergence 15%.
    Pure function — no I/O, safe for hot path.
    """
    composite = round(
        ad_score        * 0.25
        + pct_dma_score * 0.25
        + highs_lows_score * 0.20
        + sector_score  * 0.15
        + divergence_score * 0.15,
        1,
    )
    zone = _zone_for(composite)
    return {
        "composite_score":         composite,
        "zone":                    zone,
        "exposure_recommendation": _EXPOSURE_MAP[zone],
    }


def _load_universe() -> list[str]:
    """Load ticker list from stock_universe.json. Returns [] if file absent."""
    if not UNIVERSE_PATH.exists():
        return []
    data = json.loads(UNIVERSE_PATH.read_text())
    tickers: list[str] = []
    for bucket in data.get("buckets", {}).values():
        tickers.extend(bucket.get("tickers", []))
    # deduplicate while preserving order, then append .NS suffix
    return [f"{t}.NS" for t in dict.fromkeys(tickers)]


def refresh() -> dict:
    """
    Compute breadth from yfinance and write data/breadth.json.
    Network-facing — NIGHTLY ONLY, never called in hot path.
    Fetches 1-year OHLCV for universe stocks to compute all 5 components.
    """
    yf_tickers = _load_universe()
    if not yf_tickers:
        result = {"error": "stock_universe.json missing or empty"}
        BREADTH_PATH.parent.mkdir(exist_ok=True)
        BREADTH_PATH.write_text(json.dumps(result, indent=2))
        return result

    # Fetch 1-year history for all universe tickers in one call
    data = yf.download(yf_tickers, period="1y", progress=False, auto_adjust=True)
    if data is None or data.empty:
        result = {"error": "yfinance returned no data"}
        BREADTH_PATH.parent.mkdir(exist_ok=True)
        BREADTH_PATH.write_text(json.dumps(result, indent=2))
        return result

    close = data["Close"].dropna(how="all", axis=1)
    n = len(close.columns)  # number of stocks with valid data

    # --- Component 1: A/D ratio (last 2 sessions) ---
    last, prev = close.iloc[-1], close.iloc[-2]
    advancing = int((last > prev).sum())
    declining = int((last < prev).sum())

    # --- Component 2: % above 200-DMA ---
    ma200     = close.rolling(200).mean()
    above_200 = int((close.iloc[-1] > ma200.iloc[-1]).sum())
    pct_above = above_200 / n if n > 0 else 0.5

    # --- Component 3: 52-week new highs / lows ---
    high_52w  = close.rolling(252).max().iloc[-1]
    low_52w   = close.rolling(252).min().iloc[-1]
    # within 1% of 52-week high/low to count as new high/low
    new_highs = int((close.iloc[-1] >= high_52w * 0.99).sum())
    new_lows  = int((close.iloc[-1] <= low_52w  * 1.01).sum())

    # --- Component 4: Sector participation ---
    sector_greens = sector_total = 0
    for sym in SECTOR_INDICES.values():
        try:
            d = yf.download(sym, period="5d", progress=False, auto_adjust=True, multi_level_index=False)
            if d is not None and len(d) >= 2:
                sector_total += 1
                if float(d["Close"].iloc[-1]) > float(d["Close"].iloc[-2]):
                    sector_greens += 1
        except Exception:
            pass  # silently skip unavailable sector indices

    # --- Component 5: Divergence (Nifty vs breadth) ---
    nifty_positive = False
    try:
        nd = yf.download("^NSEI", period="5d", progress=False, auto_adjust=True, multi_level_index=False)
        if nd is not None and len(nd) >= 2:
            nifty_positive = float(nd["Close"].iloc[-1]) > float(nd["Close"].iloc[-2])
    except Exception:
        pass

    # Compute individual scores
    ad_s  = score_ad_ratio(advancing, declining)
    dma_s = score_pct_above_200dma(pct_above)
    hl_s  = score_new_highs_lows(new_highs, new_lows)
    sec_s = score_sector_participation(sector_greens, sector_total)
    div_s = score_divergence(nifty_positive, advancing > declining)

    composite_data = compute_composite(ad_s, dma_s, hl_s, sec_s, div_s)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **composite_data,
        "components": {
            "ad_ratio": {
                "score": ad_s, "advancing": advancing,
                "declining": declining, "weight": 0.25,
            },
            "pct_above_200dma": {
                "score": dma_s, "value": round(pct_above, 3), "weight": 0.25,
            },
            "new_highs_lows": {
                "score": hl_s, "highs": new_highs, "lows": new_lows, "weight": 0.20,
            },
            "sector_participation": {
                "score": sec_s, "green": sector_greens,
                "total": sector_total, "weight": 0.15,
            },
            "divergence": {
                "score": div_s, "nifty_up": nifty_positive, "weight": 0.15,
            },
        },
        "universe_size": n,
    }
    BREADTH_PATH.parent.mkdir(exist_ok=True)
    BREADTH_PATH.write_text(json.dumps(result, indent=2))
    return result


def load_breadth() -> dict | None:
    """Load cached breadth.json. Returns None if file absent (analogous to load_regime())."""
    if not BREADTH_PATH.exists():
        return None
    return json.loads(BREADTH_PATH.read_text())


if __name__ == "__main__":
    r = refresh()
    print(f"Breadth: {r['composite_score']} — {r['zone']}")
    print(f"Exposure: {r['exposure_recommendation']}")
