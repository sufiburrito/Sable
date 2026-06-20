# alert_bot/fundamental_score.py
"""
Fundamental quality composite scorer (1-10).

Reads market.db fundamentals table. Writes analysis/{TICKER}_fund_score.json.
The autonomous loop (Claude) also reads this score and writes it to
KNOWLEDGE_BASE/tickers/{TICKER}.md ## Current State.

Never makes network calls — fundamentals must be pre-fetched by fetch_fundamentals.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ANALYSIS_DIR = Path(__file__).parent.parent / "analysis"

# Upper bound is exclusive (lo <= score < hi) for all but the top bracket to avoid
# ambiguity at boundary scores (e.g. 7.0 must be "Good" not "Fair", regardless of
# dict iteration order). Top bracket (9, 10] uses inclusive upper to catch score=10.
SCORE_LABELS = {
    (9, 10): "Excellent",
    (7,  9): "Good",
    (5,  7): "Fair",
    (3,  5): "Weak",
    (1,  3): "Poor",
}


def _label(score: float) -> str:
    lo, hi = 9, 10
    if lo <= score <= hi:
        return "Excellent"
    for (lo, hi), label in list(SCORE_LABELS.items())[1:]:
        if lo <= score < hi:
            return label
    return "Poor"


def score_fundamentals(row: dict) -> dict:
    """
    Compute 1-10 from a fundamentals dict. Missing fields are neutral (no penalty).

    Fields: roce_pct, roe_pct, debt_equity, promoter_pledge_pct,
            revenue_growth_pct, pe_ratio.
    """
    score = 5.0

    roce = row.get("roce_pct")
    if roce is not None:
        if roce >= 25:   score += 2.0
        elif roce >= 18: score += 1.0
        elif roce >= 8:  score -= 0.5
        else:            score -= 1.5

    roe = row.get("roe_pct")
    if roe is not None:
        if roe >= 20:    score += 1.0
        elif roe < 12:   score -= 0.5

    de = row.get("debt_equity")
    if de is not None:
        if de < 0.3:     score += 1.5
        elif de < 0.7:   score += 0.5
        elif de < 1.5:   pass
        elif de < 3.0:   score -= 1.0
        else:            score -= 2.0

    pledge = row.get("promoter_pledge_pct") or 0
    if pledge >= 30:     score -= 2.0
    elif pledge >= 20:   score -= 1.0
    elif pledge >= 10:   score -= 0.5

    rev_g = row.get("revenue_growth_pct")
    if rev_g is not None:
        if rev_g >= 25:   score += 1.0
        elif rev_g >= 15: score += 0.5
        elif rev_g < 0:   score -= 1.0

    pe = row.get("pe_ratio")
    if pe is not None and pe > 80:
        score -= 0.5

    score = round(max(1.0, min(10.0, score)), 1)
    return {
        "score":        score,
        "label":        _label(score),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {k: row.get(k) for k in
                   ("roce_pct", "roe_pct", "debt_equity",
                    "promoter_pledge_pct", "revenue_growth_pct", "pe_ratio")},
    }


def run(ticker: str, db_conn=None) -> dict | None:
    """Score from market.db and write analysis/{TICKER}_fund_score.json. Returns None if no data."""
    import market_db as mdb
    conn = db_conn or mdb.get_conn()
    try:
        rows = conn.execute("""
            SELECT roce_pct, roe_pct, debt_equity, promoter_pledge_pct, pe_ratio, period
            FROM fundamentals WHERE ticker = ? AND period_type = 'annual'
            ORDER BY period DESC LIMIT 1
        """, (ticker,)).fetchall()
        if not rows:
            return None
        row_dict = dict(rows[0])
        two = conn.execute("""
            SELECT period, revenue_cr FROM fundamentals
            WHERE ticker = ? AND period_type = 'annual'
            ORDER BY period DESC LIMIT 2
        """, (ticker,)).fetchall()
        if len(two) == 2:
            curr, prev = (two[0]["revenue_cr"] or 0), (two[1]["revenue_cr"] or 0)
            row_dict["revenue_growth_pct"] = ((curr - prev) / prev * 100) if prev > 0 else None
        result = score_fundamentals(row_dict)
        ANALYSIS_DIR.mkdir(exist_ok=True)
        (ANALYSIS_DIR / f"{ticker}_fund_score.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        if db_conn is None:
            conn.close()
