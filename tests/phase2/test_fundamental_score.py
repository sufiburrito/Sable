# tests/phase2/test_fundamental_score.py
import pytest
from alert_bot.fundamental_score import score_fundamentals, SCORE_LABELS


def _row(roce=20, roe=15, debt_eq=0.3, pledge=0, rev_growth_pct=15.0, pe=25):
    return {
        "roce_pct":            roce,
        "roe_pct":             roe,
        "debt_equity":         debt_eq,
        "promoter_pledge_pct": pledge,
        "revenue_growth_pct":  rev_growth_pct,
        "pe_ratio":            pe,
    }


def test_excellent_fundamentals():
    s = score_fundamentals(_row(roce=25, roe=20, debt_eq=0.1, pledge=0, rev_growth_pct=25.0))
    assert s["score"] >= 8
    assert s["label"] == "Excellent"


def test_poor_fundamentals():
    s = score_fundamentals(_row(roce=5, roe=4, debt_eq=3.0, pledge=40, rev_growth_pct=-10.0))
    assert s["score"] <= 4


def test_pledge_above_30_reduces_score():
    high = score_fundamentals(_row(roce=25, roe=20, debt_eq=0.1, pledge=35))
    low  = score_fundamentals(_row(roce=25, roe=20, debt_eq=0.1, pledge=0))
    assert high["score"] < low["score"]


def test_missing_fields_return_neutral():
    s = score_fundamentals({})
    assert 4 <= s["score"] <= 6
    assert "label" in s


def test_score_clamped_to_1_10():
    for row in [_row(roce=30), _row(roce=3, debt_eq=5.0, pledge=50)]:
        assert 1 <= score_fundamentals(row)["score"] <= 10


def test_multiple_distinct_labels():
    labels = {score_fundamentals(_row(roce=r))["label"] for r in [5, 12, 18, 25]}
    assert len(labels) >= 3
