# tests/phase2/test_flow_regime.py
import pytest
from alert_bot.flow_regime import classify_regime, load_regime


def _make_rows(fii_vals, dii_vals):
    """Build synthetic daily_fii_dii rows, date DESC."""
    rows = []
    for i, (f, d) in enumerate(zip(fii_vals, dii_vals)):
        rows.append({
            "date": f"2026-05-{30 - i:02d}",
            "fii_net_cr": f,
            "dii_net_cr": d,
            "fii_mtd_cr": sum(fii_vals[:i+1]),
            "dii_mtd_cr": sum(dii_vals[:i+1]),
        })
    return rows


def test_dii_absorption():
    rows = _make_rows([-4500, -3900, -4200, -3600, -4100],
                      [ 3800,  3200,  3600,  3100,  3400])
    r = classify_regime(rows)
    assert r["regime"] == "DII_ABSORPTION"
    assert r["absorption_ratio"] >= 0.60
    assert r["streak_days"] >= 1


def test_dual_buying():
    rows = _make_rows([3000, 2500, 2800, 2200, 3100],
                      [1200, 1000, 1500, 1100, 1300])
    r = classify_regime(rows)
    assert r["regime"] == "DUAL_BUYING"


def test_dual_selling():
    rows = _make_rows([-2000, -1800, -2200, -1900, -2100],
                      [ -800,  -700,  -900,  -750,  -850])
    r = classify_regime(rows)
    assert r["regime"] == "DUAL_SELLING"


def test_net_buyer():
    rows = _make_rows([4000, 3500, 4200, 3800, 3900],
                      [ 200,  150,  -50,  100,  -80])
    r = classify_regime(rows)
    assert r["regime"] == "NET_BUYER"


def test_net_seller():
    rows = _make_rows([-3000, -2800, -3200, -2900, -3100],
                      [  800,   700,   900,   750,   850])
    r = classify_regime(rows)
    assert r["regime"] == "NET_SELLER"
    assert r["absorption_ratio"] < 0.60


def test_transition_low_signal():
    rows = _make_rows([200, -300, 400, -200, 100],
                      [100,  200, -50,  300,  50])
    r = classify_regime(rows)
    assert r["regime"] == "TRANSITION"


def test_empty_rows():
    r = classify_regime([])
    assert r["regime"] == "TRANSITION"
    assert r["data_points"] == 0


def test_one_liner_present():
    rows = _make_rows([-4500, -3900, -4200, -3600, -4100],
                      [ 3800,  3200,  3600,  3100,  3400])
    r = classify_regime(rows)
    assert "one_liner" in r
    assert len(r["one_liner"]) > 10


def test_load_regime_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("alert_bot.flow_regime.FLOW_REGIME_PATH", tmp_path / "flow_regime.json")
    assert load_regime() is None
