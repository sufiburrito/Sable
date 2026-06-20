# tests/phase2/test_news_impact.py
import pytest
from news_scraper import _score_news_impact


def test_macro_event_scores_high():
    score = _score_news_impact(
        headline="RBI surprise rate cut — repo rate reduced 50bp",
        source="livemint.com",
        sector="MACRO",
        causal_chain="rate cut → rate-sensitive sectors rally",
    )
    assert score >= 7


def test_regulatory_event_scores_high():
    score = _score_news_impact(
        headline="SEBI orders halt on trading of ADANI stocks",
        source="bseindia.com",
        sector="MISC",
        causal_chain="SEBI enforcement → stock suspension",
    )
    assert score >= 7


def test_analyst_upgrade_scores_low():
    score = _score_news_impact(
        headline="Analyst upgrades STLTECH target to ₹180, in line with consensus",
        source="moneycontrol.com",
        sector="TELECOM_INFRA",
        causal_chain="analyst upgrade → mild positive",
    )
    assert score <= 5


def test_telegram_rumour_penalised():
    score = _score_news_impact(
        headline="BBOX acquisition rumour doing rounds — sources say deal imminent",
        source="telegram_channel",
        sector="TELECOM_INFRA",
        causal_chain="rumoured acquisition",
    )
    # Base 3 + M&A +2 + bullish +1 - reliability -1 = 5
    assert score <= 6


def test_earnings_beat_scores_medium():
    score = _score_news_impact(
        headline="CGPOWER Q4 results beat — revenue up 32%, guidance raised",
        source="economictimes.com",
        sector="POWER_ENERGY",
        causal_chain="earnings beat → stock positive",
    )
    assert 5 <= score <= 8


def test_score_is_clamped_to_1_10():
    score = _score_news_impact(
        headline="minor update",
        source="telegram_channel",
        sector="MISC",
        causal_chain="",
    )
    assert 1 <= score <= 10


def test_multi_sector_macro_gets_breadth_bonus():
    score_macro = _score_news_impact(
        headline="Union Budget 2026 — capex allocation raised 20%",
        source="livemint.com",
        sector="MACRO",
        causal_chain="budget → multiple sectors impacted",
    )
    score_single = _score_news_impact(
        headline="SHARDACROP Q3 earnings inline",
        source="livemint.com",
        sector="AGROCHEM",
        causal_chain="inline results → neutral",
    )
    assert score_macro > score_single
