# tests/phase2/test_confidence_factors.py
import json
import numpy as np
import pandas as pd
import pytest
from pathlib import Path


def _frame(closes):
    """Build a minimal OHLC DataFrame from a list of closes (flat O/H/L)."""
    closes = list(closes)
    n = len(closes)
    return pd.DataFrame({
        "Open": closes,
        "High": closes,
        "Low": closes,
        "Close": closes,
        "Volume": [1000] * n,
    })


def _write(path, data, tmp_path):
    p = tmp_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


# --- Flow regime factor (11) ---

def test_flow_dual_buying_positive_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("data/flow_regime.json", {"regime": "DUAL_BUYING", "streak_days": 3}, tmp_path)
    from alert_bot.confidence import _score_flow_regime
    assert _score_flow_regime("BUY").score == 1


def test_flow_dual_selling_negative_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("data/flow_regime.json", {"regime": "DUAL_SELLING", "streak_days": 1}, tmp_path)
    from alert_bot.confidence import _score_flow_regime
    assert _score_flow_regime("BUY").score == -1


def test_flow_dii_absorption_positive_on_buy(tmp_path, monkeypatch):
    """DII absorption = contrarian buy signal — smart Indian money absorbing."""
    monkeypatch.chdir(tmp_path)
    _write("data/flow_regime.json", {"regime": "DII_ABSORPTION", "streak_days": 7}, tmp_path)
    from alert_bot.confidence import _score_flow_regime
    assert _score_flow_regime("BUY").score == 1


def test_flow_dual_selling_positive_on_sell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("data/flow_regime.json", {"regime": "DUAL_SELLING", "streak_days": 1}, tmp_path)
    from alert_bot.confidence import _score_flow_regime
    assert _score_flow_regime("SELL").score == 1


def test_flow_missing_file_neutral(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    from alert_bot.confidence import _score_flow_regime
    f = _score_flow_regime("BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


# --- Breadth factor (12) ---

def test_breadth_strong_positive_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("data/breadth.json", {"zone": "STRONG", "composite_score": 85}, tmp_path)
    from alert_bot.confidence import _score_breadth
    assert _score_breadth("BUY").score == 1


def test_breadth_critical_negative_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("data/breadth.json", {"zone": "CRITICAL", "composite_score": 15}, tmp_path)
    from alert_bot.confidence import _score_breadth
    assert _score_breadth("BUY").score == -1


def test_breadth_neutral_zone_is_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("data/breadth.json", {"zone": "NEUTRAL", "composite_score": 50}, tmp_path)
    from alert_bot.confidence import _score_breadth
    assert _score_breadth("BUY").score == 0


def test_breadth_strong_negative_on_sell(tmp_path, monkeypatch):
    """Strong breadth = don't sell into a healthy market."""
    monkeypatch.chdir(tmp_path)
    _write("data/breadth.json", {"zone": "STRONG", "composite_score": 85}, tmp_path)
    from alert_bot.confidence import _score_breadth
    assert _score_breadth("SELL").score == -1


# --- Fundamental factor (13) ---

def test_fundamental_high_score_positive_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("analysis/CGPOWER_fund_score.json", {"score": 8.5, "label": "Excellent"}, tmp_path)
    from alert_bot.confidence import _score_fundamental
    assert _score_fundamental("CGPOWER", "BUY").score == 1


def test_fundamental_low_score_negative_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write("analysis/IDEAFORGE_fund_score.json", {"score": 3.5, "label": "Weak"}, tmp_path)
    from alert_bot.confidence import _score_fundamental
    assert _score_fundamental("IDEAFORGE", "BUY").score == -1


def test_fundamental_high_score_negative_on_sell(tmp_path, monkeypatch):
    """Strong fundamentals = don't sell a quality compounder at support."""
    monkeypatch.chdir(tmp_path)
    _write("analysis/CGPOWER_fund_score.json", {"score": 8.5, "label": "Excellent"}, tmp_path)
    from alert_bot.confidence import _score_fundamental
    assert _score_fundamental("CGPOWER", "SELL").score == -1


def test_fundamental_missing_sidecar_neutral(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "analysis").mkdir()
    from alert_bot.confidence import _score_fundamental
    f = _score_fundamental("NOTICKER", "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


# ============================================================================
# DMA factors (14, 15, 16) — support/resistance + mean-reversion dimensions
# ============================================================================
# These factors take a pre-loaded OHLC DataFrame (not a ticker) and follow the
# _score_*(df, alert_type) -> FactorScore contract. Neutral / no-signal / no-data
# readings carry a ":n/a" label so they are excluded from the has_data conviction
# denominator (they only count when they actually cast a vote).


# --- _zscore_vs_ma helper ---

def test_zscore_vs_ma_oversold_negative():
    from alert_bot.confidence import _zscore_vs_ma
    closes = list(np.linspace(110, 100, 199)) + [88]
    z = _zscore_vs_ma(_frame(closes), 200)
    assert z is not None and z < -1.5


def test_zscore_vs_ma_insufficient_returns_none():
    from alert_bot.confidence import _zscore_vs_ma
    assert _zscore_vs_ma(_frame(np.linspace(100, 110, 150)), 200) is None


# --- Factor 14: _score_dma_support ---

def test_dma_support_on_rising_200dma_positive_on_buy():
    from alert_bot.confidence import _score_dma_support
    base = np.linspace(100, 112, 250)          # gentle uptrend → rising 200-DMA
    ma200 = pd.Series(base).rolling(200).mean().iloc[-1]
    closes = list(base)
    closes[-1] = ma200 * 1.01                   # price 1% above the rising 200-DMA
    assert _score_dma_support(_frame(closes), "BUY").score == 1


def test_dma_support_below_falling_200dma_negative_on_buy():
    from alert_bot.confidence import _score_dma_support
    base = np.linspace(150, 100, 250)          # downtrend → falling 200-DMA
    assert _score_dma_support(_frame(base), "BUY").score == -1


def test_dma_support_clear_of_dma_neutral():
    from alert_bot.confidence import _score_dma_support
    base = np.linspace(100, 140, 250)          # price ~20% above 200-DMA
    f = _score_dma_support(_frame(base), "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


def test_dma_support_resistance_from_below_positive_on_sell():
    from alert_bot.confidence import _score_dma_support
    base = np.linspace(150, 100, 250)
    ma200 = pd.Series(base).rolling(200).mean().iloc[-1]
    closes = list(base)
    closes[-1] = ma200 * 0.99                   # price 1% below 200-DMA (overhead)
    assert _score_dma_support(_frame(closes), "SELL").score == 1


def test_dma_support_insufficient_history_neutral():
    from alert_bot.confidence import _score_dma_support
    f = _score_dma_support(_frame(np.linspace(100, 110, 150)), "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


# --- Factor 15: _score_dma_extension ---

def test_dma_extension_oversold_positive_on_buy():
    from alert_bot.confidence import _score_dma_extension
    closes = list(np.linspace(110, 100, 199)) + [88]
    assert _score_dma_extension(_frame(closes), "BUY").score == 1


def test_dma_extension_overbought_positive_on_sell():
    from alert_bot.confidence import _score_dma_extension
    closes = list(np.linspace(90, 100, 199)) + [120]
    assert _score_dma_extension(_frame(closes), "SELL").score == 1


def test_dma_extension_normal_band_neutral():
    from alert_bot.confidence import _score_dma_extension
    closes = list(np.linspace(99, 101, 200))   # tight, near the mean → |z| small
    f = _score_dma_extension(_frame(closes), "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


def test_dma_extension_insufficient_history_neutral():
    from alert_bot.confidence import _score_dma_extension
    f = _score_dma_extension(_frame(np.linspace(100, 110, 150)), "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


# --- Factor 16: _score_dma_cross ---

def test_dma_golden_cross_positive_on_buy():
    from alert_bot.confidence import _score_dma_cross
    closes = list(np.linspace(120, 90, 220)) + list(np.linspace(91, 145, 30))
    assert _score_dma_cross(_frame(closes), "BUY").score == 1


def test_dma_death_cross_negative_on_buy():
    from alert_bot.confidence import _score_dma_cross
    closes = list(np.linspace(90, 120, 220)) + list(np.linspace(119, 60, 30))
    assert _score_dma_cross(_frame(closes), "BUY").score == -1


def test_dma_golden_cross_negative_on_sell():
    from alert_bot.confidence import _score_dma_cross
    closes = list(np.linspace(120, 90, 220)) + list(np.linspace(91, 145, 30))
    assert _score_dma_cross(_frame(closes), "SELL").score == -1


def test_dma_no_recent_cross_neutral():
    from alert_bot.confidence import _score_dma_cross
    closes = np.linspace(80, 160, 250)          # steady uptrend, 50>200 throughout
    f = _score_dma_cross(_frame(closes), "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


def test_dma_cross_insufficient_history_neutral():
    from alert_bot.confidence import _score_dma_cross
    f = _score_dma_cross(_frame(np.linspace(100, 110, 210)), "BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()


# --- DMA enrichment line (_format_dma_hint / dma_hint) ---

def test_format_dma_hint_above_rising():
    from alert_bot.confidence import _format_dma_hint
    s = _format_dma_hint(162 * 1.012, 162.0, 0.01)   # 1.2% above a rising 200-DMA
    assert s is not None
    assert "above" in s and "rising" in s and "200-DMA" in s
    assert "162" in s
    # the "why" clause: rising 200-DMA = institutionally-defended support
    assert "SIP" in s and "defend" in s


def test_format_dma_hint_below_falling():
    from alert_bot.confidence import _format_dma_hint
    s = _format_dma_hint(100.0, 102.0, -0.01)        # 2% below a falling 200-DMA
    assert s is not None
    assert "below" in s and "falling" in s
    # the "why" clause: falling 200-DMA = overhead resistance, not a floor
    assert "resistance" in s


def test_format_dma_hint_far_returns_none():
    from alert_bot.confidence import _format_dma_hint
    assert _format_dma_hint(200.0, 162.0, 0.01) is None   # >2.5% away → silent omit


# ============================================================================
# Phase 2 — calibration weighting (factor_weights.json → weighted composite)
# ============================================================================
# The composite was an equal-weight sum. Calibration emits per-factor weights
# (mean-normalized to 1.0). A missing/malformed file must fall back to equal
# weights so behavior is byte-identical to the pre-calibration engine.


def test_load_factor_weights_missing_file_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from alert_bot.confidence import _load_factor_weights
    assert _load_factor_weights() == {}


def test_load_factor_weights_malformed_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "factor_weights.json").write_text("{ not json")
    from alert_bot.confidence import _load_factor_weights
    assert _load_factor_weights() == {}


def test_load_factor_weights_parses_weights(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    _write("data/factor_weights.json", {"weights": {"Trend": 1.4, "Momentum": 0.7}}, tmp_path)
    from alert_bot.confidence import _load_factor_weights
    w = _load_factor_weights()
    assert w["Trend"] == 1.4 and w["Momentum"] == 0.7


def test_weighted_composite_doubles_trend():
    from alert_bot.confidence import FactorScore, _weighted_composite
    factors = [FactorScore("Trend", 1, "x"), FactorScore("Momentum", 1, "y")]
    # equal weights → 2; Trend weighted 2.0 → 3 (rounded)
    assert _weighted_composite(factors, {}) == 2
    assert _weighted_composite(factors, {"Trend": 2.0}) == 3


def test_weighted_composite_empty_weights_equals_plain_sum():
    """Cold-start regression guard: no weights ⇒ identical to equal-weight sum."""
    from alert_bot.confidence import FactorScore, _weighted_composite
    factors = [
        FactorScore("Trend", 1, "x"),
        FactorScore("Momentum", -1, "y"),
        FactorScore("Volume", 1, "z"),
        FactorScore("Regime", 0, "w"),
    ]
    plain = sum(f.score for f in factors)
    assert _weighted_composite(factors, {}) == plain


# ---------------------------------------------------------------------------
# Plain-English stats line: VCP action gloss + self-explaining backtest phrase
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (80, "good time to add"),    # ≥80 boundary
    (79, "fine to add, no rush"),
    (50, "fine to add, no rush"),  # 50 boundary
    (49, "wait, no clean entry"),
])
def test_vcp_gloss_buy_bands(score, expected):
    """BUY/WATCH glosses are entry-timing actions across the score bands."""
    from alert_bot.confidence import _vcp_gloss
    assert _vcp_gloss(score, "BUY") == expected
    assert _vcp_gloss(score, "WATCH") == expected


@pytest.mark.parametrize("score,expected", [
    (80, "breakout building, hold the trim"),  # tight coil ⇒ don't rush the trim
    (79, "trim is fine"),
    (10, "trim is fine"),
])
def test_vcp_gloss_sell_is_inverse(score, expected):
    """SELL gloss flips: a tight coil is a reason to hold the trim, not enter."""
    from alert_bot.confidence import _vcp_gloss
    assert _vcp_gloss(score, "SELL") == expected


def _stub_result(alert_type, expectancy, median_days, vcp_summary, n_agree=3, n=13):
    from alert_bot.confidence import ConfidenceResult, FactorScore
    factors = ([FactorScore(f"y{i}", 1, "x") for i in range(n_agree)]
               + [FactorScore(f"n{i}", -1, "x") for i in range(n - n_agree)])
    return ConfidenceResult(
        factors=factors, composite=1, max_score=n, verdict="WEAK", emoji="🔴",
        alert_type=alert_type, expectancy=expectancy, median_days=median_days,
        vcp_summary=vcp_summary,
    )


def test_format_stats_line_buy_plain_english():
    from alert_bot.confidence import format_stats_line
    r = _stub_result("BUY", expectancy=-4.0, median_days=1,
                     vcp_summary="VCP 51 (fine to add, no rush)")
    assert format_stats_line(r) == (
        "3/13 signals agree · VCP 51 (fine to add, no rush) · "
        "past buys here: −4% after 6 months, green within ~1 day"
    )


def test_format_stats_line_sell_uses_history_phrasing_no_green_days():
    from alert_bot.confidence import format_stats_line
    r = _stub_result("SELL", expectancy=8.0, median_days=None,
                     vcp_summary="VCP 85 (breakout building, hold the trim)")
    line = format_stats_line(r)
    assert line == (
        "3/13 signals agree · VCP 85 (breakout building, hold the trim) · "
        "history at this level: +8% over the next 6 months"
    )
    assert "past buys here" not in line and "green within" not in line


def test_format_stats_line_pluralises_days_and_bare_case():
    from alert_bot.confidence import format_stats_line
    r = _stub_result("BUY", expectancy=12.0, median_days=8, vcp_summary=None)
    assert format_stats_line(r) == (
        "3/13 signals agree · past buys here: +12% after 6 months, green within ~8 days"
    )
    # No backtest + no VCP ⇒ just the agreement count, no dangling separators.
    bare = _stub_result("BUY", expectancy=None, median_days=None, vcp_summary=None)
    assert format_stats_line(bare) == "3/13 signals agree"
