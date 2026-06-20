# alert_bot/flow_regime.py
"""
FII/DII 6-regime classifier.

Reads market.db daily_fii_dii (written by fetch_fii_dii.py) and classifies
the current institutional flow regime per docs/fii_dii_methodology.md.

Writes data/flow_regime.json for consumption by:
  - confidence.py _score_flow_regime() (Factor 11)
  - LOOP_PROMPT.md morning digest MACRO section (one_liner)

This module never makes network calls — classifies from existing DB rows only.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# Thresholds from docs/fii_dii_methodology.md
_FII_SIGNIFICANT  = 1_000   # ₹cr/day 5-day avg to call a directional regime
_FII_STRONG       = 2_000   # ₹cr/day for NET_BUYER (sustained, not just noise)
_ABSORPTION_RATIO = 0.60    # DII must cover ≥60% of FII outflow
_DUAL_THRESHOLD   = 500     # both FII and DII positive/negative above this

FLOW_REGIME_PATH = Path("data/flow_regime.json")


def classify_regime(rows: list[dict]) -> dict:
    """
    Classify FII/DII flow regime from raw daily rows (date DESC).

    Args:
        rows: list of dicts with keys fii_net_cr, dii_net_cr, fii_mtd_cr,
              dii_mtd_cr, date — as returned by market_db.query_fii_dii().
              Must be sorted date DESC (most recent first).

    Returns:
        dict with regime, streak_days, fii_5d_avg_cr, dii_5d_avg_cr,
        absorption_ratio, fii_mtd_cr, dii_mtd_cr, last_date, one_liner,
        generated_at, data_points.
    """
    if not rows:
        return {
            "generated_at":   datetime.utcnow().isoformat(),
            "regime":         "TRANSITION",
            "streak_days":    0,
            "fii_5d_avg_cr":  0.0,
            "dii_5d_avg_cr":  0.0,
            "absorption_ratio": 0.0,
            "fii_mtd_cr":     None,
            "dii_mtd_cr":     None,
            "last_date":      None,
            "one_liner":      "Regime: TRANSITION — insufficient flow data",
            "data_points":    0,
        }

    window = rows[:5]
    fii_avg = sum(r.get("fii_net_cr") or 0 for r in window) / len(window)
    dii_avg = sum(r.get("dii_net_cr") or 0 for r in window) / len(window)
    absorption_ratio = (dii_avg / abs(fii_avg)) if fii_avg < -100 else 0.0

    if fii_avg > _DUAL_THRESHOLD and dii_avg > _DUAL_THRESHOLD:
        regime = "DUAL_BUYING"
    elif fii_avg < -_DUAL_THRESHOLD and dii_avg < -_DUAL_THRESHOLD:
        regime = "DUAL_SELLING"
    elif fii_avg < -_FII_SIGNIFICANT and absorption_ratio >= _ABSORPTION_RATIO:
        regime = "DII_ABSORPTION"
    elif fii_avg > _FII_STRONG:
        regime = "NET_BUYER"
    elif fii_avg < -_FII_SIGNIFICANT:
        regime = "NET_SELLER"
    else:
        regime = "TRANSITION"

    streak  = _count_streak(rows, regime)
    latest  = rows[0]

    return {
        "generated_at":    datetime.utcnow().isoformat(),
        "regime":          regime,
        "streak_days":     streak,
        "fii_5d_avg_cr":   round(fii_avg, 1),
        "dii_5d_avg_cr":   round(dii_avg, 1),
        "absorption_ratio":round(absorption_ratio, 2),
        "fii_mtd_cr":      latest.get("fii_mtd_cr"),
        "dii_mtd_cr":      latest.get("dii_mtd_cr"),
        "last_date":       latest.get("date"),
        "one_liner":       _format_one_liner(regime, fii_avg, dii_avg, absorption_ratio, streak),
        "data_points":     len(rows),
    }


def _count_streak(rows: list[dict], target_regime: str) -> int:
    """Count consecutive days (from most recent) that would classify to target_regime."""
    streak = 0
    for i in range(len(rows)):
        window = rows[i:i+5]
        if len(window) < 5:   # stop when insufficient data for a full window
            break
        fii_avg = sum(r.get("fii_net_cr") or 0 for r in window) / len(window)
        dii_avg = sum(r.get("dii_net_cr") or 0 for r in window) / len(window)
        ar = (dii_avg / abs(fii_avg)) if fii_avg < -100 else 0.0

        if fii_avg > _DUAL_THRESHOLD and dii_avg > _DUAL_THRESHOLD:
            day_regime = "DUAL_BUYING"
        elif fii_avg < -_DUAL_THRESHOLD and dii_avg < -_DUAL_THRESHOLD:
            day_regime = "DUAL_SELLING"
        elif fii_avg < -_FII_SIGNIFICANT and ar >= _ABSORPTION_RATIO:
            day_regime = "DII_ABSORPTION"
        elif fii_avg > _FII_STRONG:
            day_regime = "NET_BUYER"
        elif fii_avg < -_FII_SIGNIFICANT:
            day_regime = "NET_SELLER"
        else:
            day_regime = "TRANSITION"

        if day_regime == target_regime:
            streak += 1
        else:
            break
    return streak


def _format_one_liner(regime: str, fii_avg: float, dii_avg: float,
                      absorption_ratio: float, streak: int) -> str:
    streak_str = f" (Day {streak})" if streak > 1 else ""
    if regime == "DII_ABSORPTION":
        return (f"Regime: DII Absorption{streak_str} — "
                f"FII {fii_avg:+.0f} cr/day absorbed {absorption_ratio*100:.0f}% by DII")
    if regime == "DUAL_BUYING":
        return (f"Regime: Dual Buying{streak_str} — "
                f"FII {fii_avg:+.0f} cr/day · DII {dii_avg:+.0f} cr/day (most powerful)")
    if regime == "DUAL_SELLING":
        return (f"Regime: Dual Selling{streak_str} — "
                f"FII {fii_avg:+.0f} cr/day · DII {dii_avg:+.0f} cr/day (systemic)")
    if regime == "NET_BUYER":
        return f"Regime: FII Net Buyer{streak_str} — FII {fii_avg:+.0f} cr/day"
    if regime == "NET_SELLER":
        return f"Regime: FII Net Seller{streak_str} — FII {fii_avg:+.0f} cr/day"
    return f"Regime: Transition{streak_str} — mixed signals"


def refresh(db_conn=None) -> dict:
    """
    Fetch rows from market.db, classify, write data/flow_regime.json.
    Returns the result dict. Safe to call from LOOP_PROMPT.md.
    """
    import market_db as mdb
    conn = db_conn or mdb.get_conn()
    try:
        rows = mdb.query_fii_dii(conn, days=30)
        result = classify_regime(rows)
        FLOW_REGIME_PATH.parent.mkdir(exist_ok=True)
        FLOW_REGIME_PATH.write_text(json.dumps(result, indent=2))
        return result
    finally:
        if db_conn is None:
            conn.close()


def load_regime() -> dict | None:
    """Read data/flow_regime.json. Returns None if file absent."""
    if not FLOW_REGIME_PATH.exists():
        return None
    return json.loads(FLOW_REGIME_PATH.read_text())


if __name__ == "__main__":
    result = refresh()
    print(result["one_liner"])
    print(f"Streak: {result['streak_days']} days | Data points: {result['data_points']}")
