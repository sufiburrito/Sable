# tests/phase2/test_calibrate.py
"""Phase 2 calibration spine — IC math, weight normalization, reconstruction causality."""
import numpy as np
import pandas as pd
import pytest


def _frame(closes):
    """Minimal OHLC DataFrame from a list of closes (flat O/H/L), Date-indexed."""
    closes = list(closes)
    n = len(closes)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1000] * n,
        },
        index=idx,
    )


# --- spearman_ic -----------------------------------------------------------

def test_spearman_ic_monotonic_positive():
    from alert_bot.calibrate import spearman_ic
    scores = list(range(40))
    returns = [s * 2.0 for s in scores]          # perfectly rank-aligned
    ic = spearman_ic(scores, returns, min_samples=30)
    assert ic is not None and ic > 0.99


def test_spearman_ic_monotonic_negative():
    from alert_bot.calibrate import spearman_ic
    scores = list(range(40))
    returns = [-s for s in scores]               # perfectly anti-aligned
    ic = spearman_ic(scores, returns, min_samples=30)
    assert ic is not None and ic < -0.99


def test_spearman_ic_handles_discrete_ties():
    """Factor scores are -1/0/+1 → heavy ties; must use average-rank Spearman."""
    from alert_bot.calibrate import spearman_ic
    rng = np.random.default_rng(0)
    scores, returns = [], []
    for _ in range(300):
        s = rng.choice([-1, 0, 1])
        scores.append(int(s))
        returns.append(float(s) * 3 + rng.normal())   # signal + noise
    ic = spearman_ic(scores, returns, min_samples=30)
    assert ic is not None and ic > 0.3              # clear positive signal survives ties


def test_spearman_ic_too_few_samples_none():
    from alert_bot.calibrate import spearman_ic
    assert spearman_ic([1, 0, -1], [1.0, 0.0, -1.0], min_samples=30) is None


def test_spearman_ic_zero_variance_none():
    from alert_bot.calibrate import spearman_ic
    assert spearman_ic([0] * 40, list(range(40)), min_samples=30) is None


# --- blended_forward_return ------------------------------------------------

def test_blended_forward_return_averages_windows():
    from alert_bot.calibrate import blended_forward_return
    # 260 bars: entry at i=0 price 100; closes rise linearly so each window has a
    # known return. Use a flat-then-step series for an exact check instead.
    closes = [100.0] * 260
    closes[63] = 110.0     # +10% at 63d
    closes[126] = 120.0    # +20% at 126d
    closes[252] = 130.0    # +30% at 252d
    r = blended_forward_return(closes, 0, windows=(63, 126, 252))
    assert r is not None and abs(r - (10 + 20 + 30) / 3) < 1e-6


def test_blended_forward_return_insufficient_none():
    from alert_bot.calibrate import blended_forward_return
    closes = [100.0] * 200          # not enough for a 252d forward window
    assert blended_forward_return(closes, 0, windows=(63, 126, 252)) is None


# --- ic_to_weights ---------------------------------------------------------

def test_ic_to_weights_calibrated_mean_is_one():
    from alert_bot.calibrate import ic_to_weights
    ic = {"A": 0.20, "B": 0.10, "C": 0.00}
    n = {"A": 500, "B": 500, "C": 500}
    w = ic_to_weights(ic, n, min_samples=30, ic_floor=0.02)
    # C is below the IC floor → fixed at 1.0; A and B are calibrated → mean 1.0
    assert w["C"] == 1.0
    assert abs((w["A"] + w["B"]) / 2 - 1.0) < 1e-9
    assert w["A"] > w["B"]               # higher IC = louder vote


def test_ic_to_weights_low_samples_neutral():
    from alert_bot.calibrate import ic_to_weights
    ic = {"A": 0.40}
    n = {"A": 5}                         # below sample floor
    w = ic_to_weights(ic, n, min_samples=30, ic_floor=0.02)
    assert w["A"] == 1.0


# --- reconstruction causality (no look-ahead) ------------------------------

def test_score_functions_are_causal():
    """Truncating to df[:t] must give the same score whether or not future bars exist.
    This is the premise that makes historical factor reconstruction valid."""
    from alert_bot.confidence import _score_trend, _score_dma_support, _score_dma_extension
    rng = np.random.default_rng(1)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 400)))
    full = _frame(closes)
    t = 300
    past_only = _frame(closes[:t])
    for fn in (_score_trend, _score_dma_support, _score_dma_extension):
        a = fn(full.iloc[:t], "BUY")
        b = fn(past_only, "BUY")
        assert a.score == b.score, f"{fn.__name__} peeked at future bars"


def test_relative_strength_accepts_explicit_nifty():
    """RS reconstruction must pass a date-aligned Nifty slice (no disk look-ahead)."""
    from alert_bot.confidence import _score_relative_strength
    stock = _frame(list(np.linspace(100, 130, 80)))      # +30% over window
    nifty = _frame(list(np.linspace(100, 105, 80)))      # +5% over window
    f = _score_relative_strength(stock, "BUY", nifty=nifty)
    assert f.score == 1 and "Nifty" in f.label


# --- Deliverable 1: fire-time factor-vector logging ------------------------

def _conf():
    from alert_bot.confidence import ConfidenceResult, FactorScore
    return ConfidenceResult(
        factors=[FactorScore("Trend", 1, "x"), FactorScore("Momentum", -1, "y")],
        composite=0, max_score=2, verdict="MODERATE", emoji="🟡", alert_type="BUY",
    )


def test_register_persists_factor_vector(tmp_path):
    from datetime import datetime, timezone
    from alert_bot.feedback import SentAlertsRegistry
    reg = SentAlertsRegistry(tmp_path / "sent_alerts.json")
    reg.register(123, "CGPOWER", "BUY", "₹100", 100.0, "🟢", 2, "msg", "claude",
                 datetime.now(timezone.utc), confidence_result=_conf())
    rec = reg.lookup(123)
    assert rec["factors"] == {"Trend": 1, "Momentum": -1}
    assert rec["composite"] == 0 and rec["verdict"] == "MODERATE" and rec["max_score"] == 2


def test_register_without_conf_backward_compatible(tmp_path):
    from datetime import datetime, timezone
    from alert_bot.feedback import SentAlertsRegistry
    reg = SentAlertsRegistry(tmp_path / "sent_alerts.json")
    reg.register(1, "X", "BUY", "₹1", 1.0, "🟢", 2, "m", "claude", datetime.now(timezone.utc))
    rec = reg.lookup(1)
    assert "factors" not in rec and rec["confidence"] == 2
