"""
Forward-test rig: resolver R-math + level-reconstruction sanity.

The resolver is the integrity-critical piece — it turns a call + future OHLC into a
realized R. These pin the four outcomes (target/stop/same-bar/time-cap), the
"not enough sessions yet" hold, and the SELL mirror.
"""
import pandas as pd

import forward_lib as fl
from forward_resolve import resolve_row


def _df(bars, start="2025-01-02"):
    """bars: list of (high, low, close) → daily OHLC frame from `start`."""
    idx = pd.bdate_range(start, periods=len(bars))
    return pd.DataFrame(
        {"High": [b[0] for b in bars], "Low": [b[1] for b in bars],
         "Close": [b[2] for b in bars]}, index=idx)


def _row(atype="BUY", entry=100.0, target=110.0, stop=95.0, rr=2.0):
    return {"fired_at": "2025-01-01T09:15:00+05:30", "alert_type": atype,
            "entry": entry, "target": target, "stop": stop, "rr": rr, "status": "open"}


def test_excursion_peak_trough_and_completeness():
    df = _df([(105, 99, 104), (112, 100, 110), (108, 95, 96)])   # 3 bars after the ref date
    e = fl.excursion(df, "2025-01-01", 100.0, horizon=3)
    assert e["peak_pct"] == 12.0 and e["trough_pct"] == -5.0     # max High 112, min Low 95
    assert e["bars"] == 3 and e["complete"] is True


def test_excursion_incomplete_window_is_pending():
    df = _df([(102, 99, 101)])                                   # only 1 bar of a 63 horizon
    e = fl.excursion(df, "2025-01-01", 100.0)                    # default horizon = HORIZON_CAP
    assert e["bars"] == 1 and e["complete"] is False


def test_excursion_none_when_no_future_bars_or_no_ref():
    df = _df([(102, 99, 101)], start="2025-01-02")
    assert fl.excursion(df, "2030-01-01", 100.0) is None         # ref date after all bars
    assert fl.excursion(df, "2024-01-01", 0) is None             # no ref price
    assert fl.excursion(None, "2025-01-01", 100.0) is None       # no OHLC


def test_first_touch_low_reached_includes_fire_date():
    # window is INCLUSIVE of the from_date (the alert fires when price is at the level)
    df = _df([(105, 98, 104), (107, 101, 106)], start="2025-01-02")
    hit, lo = fl.first_touch_low(df, "2025-01-02", 100.0)        # Low 98 ≤ 100 on day 1
    assert hit is True and lo == 98.0


def test_first_touch_low_not_reached_returns_closest():
    df = _df([(112, 106, 110), (115, 108, 113)], start="2025-01-02")
    hit, lo = fl.first_touch_low(df, "2025-01-02", 100.0)        # never dips to 100
    assert hit is False and lo == 106.0                          # closest it got = min Low


def test_first_touch_low_respects_the_window():
    # a dip to the level AFTER the 45-day window must NOT count as reached
    bars = [(112, 106, 110)] * 40 + [(101, 95, 99)]             # day ~41 dips to 95
    df = _df(bars, start="2025-01-01")
    hit, lo = fl.first_touch_low(df, "2025-01-01", 100.0, days=45)
    # 40 business days ≈ 56 calendar days, so the dip is outside 45 calendar days → not reached
    assert hit is False and lo == 106.0     # closest = the flat Low inside the window


def test_first_touch_low_none_guards():
    df = _df([(102, 99, 101)])
    assert fl.first_touch_low(None, "2025-01-01", 100.0) == (None, None)
    assert fl.first_touch_low(df, "2025-01-01", 0) == (None, None)


def test_buy_target_hit_is_win_at_rr():
    row = _row(rr=2.0)
    df = _df([(105, 99, 104), (111, 108, 110)])      # bar 2 tags target 110
    assert resolve_row(row, df) is True
    assert row["status"] == "win" and row["realized_R"] == 2.0
    assert row["exit_reason"] == "target"


def test_buy_stop_hit_is_loss_minus_one():
    row = _row()
    df = _df([(102, 96, 98), (99, 94, 95)])          # bar 2 tags stop 95
    assert resolve_row(row, df) is True
    assert row["status"] == "loss" and row["realized_R"] == -1.0


def test_buy_same_bar_both_is_pessimistic_loss():
    row = _row()
    df = _df([(112, 94, 100)])                        # one bar spans stop AND target
    resolve_row(row, df)
    assert row["status"] == "loss" and row["exit_reason"] == "stop"


def test_buy_time_cap_is_fractional_R():
    # 63 flat-ish bars, never touching 110 or 95; close 102 → (102-100)/(100-95)=0.4
    row = _row()
    df = _df([(103, 99, 102)] * fl.HORIZON_CAP)
    resolve_row(row, df)
    assert row["exit_reason"] == "time_cap"
    assert row["realized_R"] == 0.4 and row["status"] == "win"


def test_not_enough_sessions_stays_open():
    row = _row()
    df = _df([(103, 99, 102)] * 5)                    # < HORIZON_CAP, no hit
    assert resolve_row(row, df) is False
    assert row["status"] == "open"


def test_sell_mirror_reload_hit_is_win():
    # SELL: target=reload (below), stop=resistance (above).
    row = _row(atype="SELL", entry=100.0, target=90.0, stop=108.0, rr=1.25)
    df = _df([(102, 98, 101), (101, 89, 90)])         # bar 2 tags reload 90
    resolve_row(row, df)
    assert row["status"] == "win" and row["realized_R"] == 1.25


def test_sell_mirror_resistance_hit_is_loss():
    row = _row(atype="SELL", entry=100.0, target=90.0, stop=108.0, rr=1.25)
    df = _df([(109, 101, 107)])                       # tags resistance 108
    resolve_row(row, df)
    assert row["status"] == "loss" and row["realized_R"] == -1.0


def test_reconstruct_buy_target_is_capped_realistic():
    """A giant backtest MFE must not project an absurd target — the p75 cone caps it."""
    import numpy as np
    idx = pd.bdate_range("2022-01-03", periods=300)
    px = 500 + np.cumsum(np.random.default_rng(0).normal(0, 5, 300))
    df = pd.DataFrame({"Open": px, "High": px + 3, "Low": px - 3, "Close": px,
                       "Volume": [1_000_000] * 300}, index=idx)
    entry = float(px[-1])
    out = fl.reconstruct_buy(entry, df, "bull", {"mfe_6m": 600.0, "n": 5})  # absurd MFE
    assert out  # produced a level
    reward_pct = (out["target"] - entry) / entry * 100
    assert reward_pct < 60          # capped, not +600%
    assert out["stop"] < entry < out["target"]


# ── estimator: Bayesian posterior + regime discount ─────────────────────────

def _closed(status, R, reg="bull", bt=None, conv=2, atype="BUY"):
    return {"status": status, "realized_R": R, "regime_at_fire": reg,
            "bt_winrate": bt, "conviction": conv, "alert_type": atype, "liq_tier": "liquid"}


def test_regime_discount_shrinks_toward_one():
    import forward_edge as fe
    # forward 100% wins vs backtest 50% → raw δ=2.0, but shrunk toward 1 with few obs.
    rows = [_closed("win", 1.0, bt=0.5) for _ in range(4)]
    d = fe._regime_discount(rows)["bull"]
    assert 1.0 < d < 2.0            # pulled in from the raw 2.0
    # no backtest anchor → δ defaults to 1.0
    assert fe._regime_discount([_closed("win", 1.0, bt=None)])["bull"] == 1.0


def test_posterior_flags_real_edge_and_excludes_zero():
    import forward_edge as fe
    rows = [_closed("win", 0.8) for _ in range(8)] + [_closed("loss", -1.0)]
    p = fe._posterior(rows, {"bull": 1.0})
    assert p["P_win_gt_50"] > 0.9 and p["P_edge_gt_0"] > 0.9
    assert p["exp_ci"][0] > 0       # 90% expectancy interval excludes zero


def test_posterior_skeptical_when_no_backtest_prior():
    import forward_edge as fe
    rows = [_closed("win", 0.5, bt=None), _closed("loss", -1.0, bt=None)]
    p = fe._posterior(rows, {"bull": 1.0})
    assert p["p_prior"] == 0.5       # falls back to the skeptical 0.5 prior
    assert p["wr_ci"][0] < p["wr_mean"] < p["wr_ci"][1]


# ── consolidated runner: stage isolation ────────────────────────────────────

def test_forward_test_stage_reports_success_and_failure():
    import forward_test as ft
    assert ft._stage("ok", lambda: None) is True

    def boom():
        raise RuntimeError("kaboom")
    assert ft._stage("bad", boom) is False     # a raising stage is caught, not propagated
