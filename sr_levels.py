"""
sr_levels.py — historical support/resistance for the analysis PDF.

Two complementary, standard methods, computed at render time from a ticker's
OHLC cache so the report is consistent for every stock (no reliance on
hand-authored levels):

  A) Swing detection + clustering by touch count (empirical — where price
     actually turned). A swing point is a local High/Low over a ±window; nearby
     swings are merged into a zone, and the number of swings in a zone is its
     "touch count" = strength. Zones below the current price read as support,
     above as resistance (a level's polarity flips around price — classic S/R).

  B) Fibonacci retracements (derived — levels the market self-fulfils), anchored
     on the trailing 52-week range. A Fibonacci level that coincides with a
     touch-tested swing zone is "confluence" — the strongest kind of level.

This file deliberately carries its own small copy of the swing/cluster logic
(mirrored from export_stock.py) so the PDF path stays decoupled from that CLI
module's heavier imports. (Follow-up: dedupe onto this module.)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

SWING_WINDOW = 5          # candles either side for a local extreme
CLUSTER_PCT = 0.025       # merge pivots within 2.5% of each other
FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)
CONFLUENCE_PCT = 0.01     # a Fib within 1% of a swing zone = confluence
LOOKBACK_52W = 252        # trading days ≈ one year, for the Fibonacci anchor


def _detect_pivots(df: pd.DataFrame, window: int) -> list[tuple]:
    """Every swing high and swing low as (date, price) — a local extreme over
    [i-window .. i+window]. Highs and lows are pooled: a price that has acted as
    both a top and a bottom is a stronger level, and pooling lets the touch count
    capture that."""
    pivots: list[tuple] = []
    n = len(df)
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    for i in range(window, n - window):
        h = float(highs[i])
        l = float(lows[i])
        if h >= float(highs[i - window: i + window + 1].max()):
            pivots.append((df.index[i], h))
        if l <= float(lows[i - window: i + window + 1].min()):
            pivots.append((df.index[i], l))
    return pivots


def _cluster(pivots: list[tuple], cluster_pct: float) -> list[dict]:
    """Group pivots within cluster_pct of each other into price zones.
    Each zone: {price, low, high, touches, latest_date}."""
    if not pivots:
        return []
    pts = sorted(pivots, key=lambda x: x[1])
    groups: list[list[tuple]] = [[pts[0]]]
    for pt in pts[1:]:
        ref = sum(p[1] for p in groups[-1]) / len(groups[-1])
        if abs(pt[1] - ref) / ref <= cluster_pct:
            groups[-1].append(pt)
        else:
            groups.append([pt])

    zones = []
    for g in groups:
        prices = [p[1] for p in g]
        latest = max(p[0] for p in g)
        zones.append({
            "price": round(sum(prices) / len(prices)),
            "low": round(min(prices)),
            "high": round(max(prices)),
            "touches": len(g),
            "latest_date": latest.strftime("%b %Y") if hasattr(latest, "strftime") else str(latest),
        })
    return zones


def _fib_levels(low: float, high: float, current_price: float,
                swing_zones: list[dict]) -> list[dict]:
    """Fibonacci retracements off the [low, high] range, classified as support
    (below current price) or resistance (above), with confluence to any swing
    zone within CONFLUENCE_PCT."""
    rng = high - low
    if rng <= 0:
        return []
    out = []
    for r in FIB_RATIOS:
        price = round(high - rng * r)
        # Confluence only counts when it lands on a *real* zone (≥2 touches);
        # pick the strongest (most-touched) coinciding zone.
        matches = [z["touches"] for z in swing_zones
                   if z["touches"] >= 2 and z["price"]
                   and abs(price - z["price"]) / z["price"] <= CONFLUENCE_PCT]
        confl = max(matches) if matches else None
        out.append({
            "ratio": r,
            "price": price,
            "type": "support" if price < current_price else "resistance",
            "confluence": confl,
        })
    return out


def compute_sr(ohlc_csv_path: "str | Path | pd.DataFrame", current_price: float,
               window: int = SWING_WINDOW, cluster_pct: float = CLUSTER_PCT,
               max_per_side: int = 5) -> dict:
    """
    Compute historical support/resistance for a ticker from its OHLC cache.

    Returns {"support": [...], "resistance": [...], "fib": [...]} where support
    and resistance are touch-tested swing zones nearest the current price first,
    and fib is the five 52-week retracement levels. Returns empty lists on any
    failure or too-little history — the caller renders nothing in that case.
    """
    try:
        if isinstance(ohlc_csv_path, pd.DataFrame):
            df = ohlc_csv_path                      # in-memory frame (e.g. as-of slice)
        else:
            path = Path(ohlc_csv_path)
            if not path.exists():
                return {"support": [], "resistance": [], "fib": []}
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
        if len(df) < 2 * window + 1:
            return {"support": [], "resistance": [], "fib": []}

        # Fall back to the cache's last close when the caller has no live price
        # (some data JSONs omit current_price) — keeps the section self-contained.
        if not current_price:
            current_price = float(df["Close"].iloc[-1])

        zones = _cluster(_detect_pivots(df, window), cluster_pct)

        # Split by current price; nearest-first; prefer multi-touch zones but
        # fall back to single-touch if a side would otherwise be empty.
        below = sorted((z for z in zones if z["price"] < current_price),
                       key=lambda z: -z["price"])
        above = sorted((z for z in zones if z["price"] > current_price),
                       key=lambda z: z["price"])

        def _pick(side: list[dict]) -> list[dict]:
            strong = [z for z in side if z["touches"] >= 2]
            chosen = (strong or side)[:max_per_side]
            omitted = len(side) - len(chosen)
            for z in chosen:
                z["omitted_after"] = omitted  # informational; same for the group
            return chosen

        support = _pick(below)
        resistance = _pick(above)

        recent = df.tail(LOOKBACK_52W)
        lo = float(recent["Low"].min())
        hi = float(recent["High"].max())
        fib = _fib_levels(lo, hi, current_price, zones)

        return {"support": support, "resistance": resistance, "fib": fib}
    except Exception:
        return {"support": [], "resistance": [], "fib": []}
