# tests/phase2/test_breadth_score.py
import numpy as np
import pandas as pd
import pytest
from alert_bot.breadth_score import (
    score_ad_ratio, score_pct_above_200dma, score_new_highs_lows,
    score_sector_participation, score_divergence, compute_composite,
)


def test_score_ad_ratio_bull_market():
    s = score_ad_ratio(advancing=80, declining=20)
    assert s >= 75


def test_score_ad_ratio_bear_market():
    s = score_ad_ratio(advancing=20, declining=80)
    assert s <= 20


def test_score_ad_ratio_equal():
    s = score_ad_ratio(advancing=50, declining=50)
    assert 45 <= s <= 55


def test_score_pct_above_200dma_bull():
    assert score_pct_above_200dma(0.70) >= 60


def test_score_pct_above_200dma_bear():
    assert score_pct_above_200dma(0.25) <= 40


def test_score_new_highs_lows_positive():
    assert score_new_highs_lows(highs=80, lows=10) >= 70


def test_score_new_highs_lows_negative():
    assert score_new_highs_lows(highs=5, lows=60) <= 30


def test_zone_strong_at_high_scores():
    result = compute_composite(90, 90, 90, 90, 80)
    assert result["zone"] == "STRONG"
    assert result["composite_score"] >= 80


def test_zone_critical_at_low_scores():
    result = compute_composite(10, 10, 10, 20, 20)
    assert result["zone"] == "CRITICAL"
    assert result["composite_score"] < 20


def test_zone_healthy_mid_range():
    result = compute_composite(75, 70, 65, 60, 55)
    assert result["zone"] in ("HEALTHY", "STRONG")
    assert result["composite_score"] >= 60


def test_divergence_confirmed_bull():
    assert score_divergence(nifty_positive=True, breadth_positive=True) >= 75


def test_divergence_bearish_warning():
    """Index up but breadth weak — bearish divergence."""
    assert score_divergence(nifty_positive=True, breadth_positive=False) <= 35


def test_exposure_recommendation_present():
    result = compute_composite(75, 70, 65, 60, 55)
    assert "exposure_recommendation" in result
    assert "%" in result["exposure_recommendation"]


def test_score_sector_participation_all_green():
    assert score_sector_participation(8, 8) >= 85


def test_score_sector_participation_all_red():
    assert score_sector_participation(0, 8) <= 25


def test_score_sector_participation_no_data():
    # When total_count == 0 (all fetches failed), should return neutral 50
    assert score_sector_participation(0, 0) == 50.0
