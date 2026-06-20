# tests/phase2/test_fno_signals.py
import json
import pytest
from pathlib import Path


def _write_fno(tmp_path, vix_value):
    p = tmp_path / "data" / "fno_signals.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"vix": {"value": vix_value}}))


def test_vix_above_25_is_bullish_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_fno(tmp_path, 28.0)
    from alert_bot.confidence import _score_vix
    f = _score_vix("BUY")
    assert f.score == 1
    assert "28" in f.label


def test_vix_below_12_is_bearish_on_buy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_fno(tmp_path, 10.5)
    from alert_bot.confidence import _score_vix
    assert _score_vix("BUY").score == -1


def test_vix_normal_is_neutral(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_fno(tmp_path, 17.0)
    from alert_bot.confidence import _score_vix
    assert _score_vix("BUY").score == 0


def test_vix_above_25_is_bearish_on_sell(tmp_path, monkeypatch):
    """High VIX = don't sell into fear — likely near bottom."""
    monkeypatch.chdir(tmp_path)
    _write_fno(tmp_path, 30.0)
    from alert_bot.confidence import _score_vix
    assert _score_vix("SELL").score == -1


def test_vix_below_12_is_bullish_on_sell(tmp_path, monkeypatch):
    """Complacency = good time to trim."""
    monkeypatch.chdir(tmp_path)
    _write_fno(tmp_path, 11.0)
    from alert_bot.confidence import _score_vix
    assert _score_vix("SELL").score == 1


def test_vix_crisis_above_35_is_bearish_on_buy(tmp_path, monkeypatch):
    """Crisis VIX — cash heavy per docs/fno_signals.md."""
    monkeypatch.chdir(tmp_path)
    _write_fno(tmp_path, 38.0)
    from alert_bot.confidence import _score_vix
    assert _score_vix("BUY").score == -1


def test_missing_fno_file_returns_neutral(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    from alert_bot.confidence import _score_vix
    f = _score_vix("BUY")
    assert f.score == 0
    assert "n/a" in f.label.lower()
