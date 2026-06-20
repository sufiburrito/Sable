"""
Trade Journal — Phase A: missed-trade matching + classification.

Pins the integrity-critical logic: was a Sable call taken (date+price window), and
if not, was it a missed winner, a dodged loser, or still pending. Two-sided by design.
"""
from datetime import date

from journal.missed_trades import was_taken, classify, build_missed, _corroboration


def _call(ticker="X", entry=100.0, status="win", r=2.0, atype="BUY",
          fired="2026-03-13T09:00:00+05:30", target=120.0, stop=90.0):
    return {"ticker": ticker, "alert_type": atype, "entry": entry, "target": target,
            "stop": stop, "rr": 2.0, "status": status, "realized_R": r,
            "fired_at": fired, "resolved_at": "2026-03-20", "conviction": 2,
            "regime_at_fire": "bull", "exit_reason": "target"}


# ── was_taken: date + price window ──────────────────────────────────────────

def test_taken_when_bought_near_level_and_date():
    buys = {"X": [(date(2026, 3, 15), 101.0)]}           # +2 days, +1% — same trade
    assert was_taken(_call(), buys) is True


def test_not_taken_when_outside_date_window():
    buys = {"X": [(date(2026, 4, 30), 100.0)]}           # ~7 weeks later
    assert was_taken(_call(), buys) is False


def test_not_taken_when_price_too_far():
    buys = {"X": [(date(2026, 3, 14), 130.0)]}           # +30% — a different entry
    assert was_taken(_call(), buys) is False


def test_not_taken_when_symbol_absent():
    assert was_taken(_call(), {"OTHER": [(date(2026, 3, 13), 100.0)]}) is False


# ── classify: outcome → label + counterfactual % ────────────────────────────

def test_classify_missed_winner():
    label, pct = classify(_call(status="win", r=2.0))   # risk 10% × 2R = +20%
    assert label == "missed_winner" and pct == 20.0


def test_classify_dodged_loser():
    label, pct = classify(_call(status="loss", r=-1.0))
    assert label == "dodged_loser" and pct == -10.0


def test_classify_pending_when_open():
    label, pct = classify(_call(status="open", r=None))
    assert label == "pending" and pct is None


# ── build_missed: filtering, exclusion, dedup ───────────────────────────────

def test_taken_calls_are_excluded():
    buys = {"X": [(date(2026, 3, 13), 100.0)]}
    assert build_missed([_call()], buys) == []          # taken → not in the missed list


def test_sell_calls_ignored():
    assert build_missed([_call(atype="SELL")], {}) == []


def test_dedup_collapses_same_ticker_day_level():
    # Four near-identical re-fires of one opportunity → a single missed row.
    calls = [_call(entry=e) for e in (125.94, 125.97, 125.67, 125.84)]
    out = build_missed(calls, {})
    assert len(out) == 1 and out[0]["outcome"] == "missed_winner"


def test_distinct_dates_stay_separate():
    a = _call(fired="2026-03-27T09:00:00+05:30")
    b = _call(fired="2026-04-02T09:00:00+05:30")
    assert len(build_missed([a, b], {})) == 2


# ── corroboration: ground the verdict in real price ─────────────────────────

def test_corroboration_target_hit_is_real_winner():
    assert _corroboration({"exit_reason": "target"}, "missed_winner") == "target_hit"


def test_corroboration_stop_is_real_dodge():
    assert _corroboration({"exit_reason": "stop"}, "dodged_loser") == "stopped"


def test_corroboration_time_cap_is_soft_theoretical():
    # target never printed — a "win" only by mark-to-market, not a real capture
    assert _corroboration({"exit_reason": "time_cap"}, "missed_winner") == "soft"


def test_corroboration_pending_when_unresolved():
    assert _corroboration({"exit_reason": None}, "pending") == "pending"
