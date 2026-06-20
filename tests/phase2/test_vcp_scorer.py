# tests/phase2/test_vcp_scorer.py
import json
import math
import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from alert_bot.vcp_scorer import compute_vcp_bundle, score_factor


def _make_df(n=250, trend="up"):
    """Synthetic OHLCV — uptrend with declining volume (VCP pattern)."""
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    base = np.linspace(100, 200, n) if trend == "up" else np.linspace(200, 100, n)
    rng  = np.random.default_rng(42)
    close = base + rng.normal(0, 2, n)
    high  = close + np.abs(np.random.default_rng(1).normal(0, 2, n))
    low   = close - np.abs(np.random.default_rng(2).normal(0, 2, n))
    vol   = np.linspace(1_000_000, 400_000, n)
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=dates)


def test_compute_vcp_bundle_returns_required_keys():
    df = _make_df()
    result = compute_vcp_bundle(df, curr_price=180.0, ticker="TESTCO")
    for key in ("composite_score", "is_vcp", "stage", "dry_up_ratio", "components"):
        assert key in result
    assert 0 <= result["composite_score"] <= 100


def test_score_factor_high_composite_positive_on_buy():
    result = score_factor(composite=85.0, is_vcp=True, alert_type="BUY")
    assert result.score == 1
    assert "85" in result.label


def test_score_factor_low_composite_negative_on_buy():
    result = score_factor(composite=30.0, is_vcp=False, alert_type="BUY")
    assert result.score == -1


def test_score_factor_high_composite_negative_on_sell():
    result = score_factor(composite=85.0, is_vcp=True, alert_type="SELL")
    assert result.score == -1


def test_score_factor_neutral_on_watch():
    result = score_factor(composite=85.0, is_vcp=True, alert_type="WATCH")
    assert result.score == 0


def test_score_vcp_returns_na_when_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "analysis").mkdir()
    from alert_bot.confidence import _score_vcp
    factor = _score_vcp("NOTICKER", "BUY", vcp_data=None)
    assert factor.score == 0
    assert "n/a" in factor.label.lower()


def test_score_vcp_reads_preloaded_data():
    """When vcp_data is passed, no file read occurs — data is used directly."""
    from alert_bot.confidence import _score_vcp
    vcp_data = {"composite_score": 88.0, "is_vcp": True, "pivot": 782.0}
    factor = _score_vcp("NETWEB", "BUY", vcp_data=vcp_data)
    assert factor.score == 1
    assert "88" in factor.label


def test_composite_verdict_scales_with_n_factors():
    """_composite_verdict must yield HIGH CONVICTION at ~75% regardless of factor count."""
    from alert_bot.confidence import _composite_verdict
    # 8 factors: 6/8 = 75% → HIGH CONVICTION
    verdict_8, _ = _composite_verdict(6, 8, "BUY")
    assert verdict_8 == "HIGH CONVICTION"
    # 13 factors: 10/13 ≈ 77% → HIGH CONVICTION
    verdict_13, _ = _composite_verdict(10, 13, "BUY")
    assert verdict_13 == "HIGH CONVICTION"
    # 13 factors: 5/13 ≈ 38% → BUILDING or WEAK (below MODERATE)
    verdict_low, _ = _composite_verdict(5, 13, "BUY")
    assert verdict_low in ("BUILDING", "WEAK")
