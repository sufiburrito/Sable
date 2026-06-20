"""
Unit tests for alert_bot.trade_levels — the robust target/stop deriver.

Methodology under test (bean algotrading-4eon):
  - stop  = wider/safer of {ATR floor, nearest structural support}; deepest rung → "daily close below"
  - target = level MFE shrunk toward the stock's pooled floor-run-up (w = n/(n+k)),
             capped at the regime Monte-Carlo p75 cone
  - R:R computed in Python; the TRADE: swing line is emitted ONLY at/above RR_FLOOR — a weak
    swing keeps no TRADE: line and stays a pure long-term investment alert (no "wait" hedge)
  - format routed per level: numeric swing | etf_band | scenario (while Binary-phase PENDING) | trim
  - range cells: BUY uses the UPPER bound, SELL the LOWER (matches the live engine's trigger bound)

Pure helpers are tested directly; derive_levels() is tested with injected fixtures
(no file IO, no network, no pandas) so the contract is pinned without the data layer.
"""
from types import SimpleNamespace

import pytest

from alert_bot import trade_levels as tl


# ---------------------------------------------------------------------------
# Fixtures — lightweight stand-ins for parser.AlertLevel / StockConfig
# ---------------------------------------------------------------------------

def _lvl(price_str, atype, lower, upper, signal="🟢", message="base msg"):
    return SimpleNamespace(
        signal=signal, price_str=price_str, lower=lower, upper=upper,
        alert_type=atype, message=message, confidence=2,
    )


def _stock(ticker, levels, core_pct=0):
    return SimpleNamespace(ticker=ticker, yf_symbol=f"{ticker}.NS",
                           name=ticker, core_pct=core_pct, levels=levels)


# ---------------------------------------------------------------------------
# shrinkage
# ---------------------------------------------------------------------------

def test_shrink_weight_half_at_n_equals_k():
    assert tl.shrink_weight(10, k=10) == pytest.approx(0.5)


def test_shrink_weight_zero_for_no_sample():
    assert tl.shrink_weight(0, k=10) == 0.0


def test_shrink_weight_rises_with_n():
    assert tl.shrink_weight(90, k=10) == pytest.approx(0.9)


def test_shrunk_mfe_blends_toward_pooled_prior():
    # n=10, k=10 → w=0.5; halfway between level 30 and pooled 10 → 20
    assert tl.shrunk_mfe(30.0, 10, 10.0) == pytest.approx(20.0)


def test_shrunk_mfe_uses_pooled_when_level_missing():
    assert tl.shrunk_mfe(None, 0, 14.0) == pytest.approx(14.0)


def test_shrunk_mfe_uses_level_when_pooled_missing():
    assert tl.shrunk_mfe(22.0, 8, None) == pytest.approx(22.0)


def test_shrunk_mfe_none_when_no_data():
    assert tl.shrunk_mfe(None, 0, None) is None


# ---------------------------------------------------------------------------
# target — shrink then cap at MC p75 cone
# ---------------------------------------------------------------------------

def test_buy_target_projects_from_shrunk_mfe():
    # entry 100, shrunk mfe 20% → 120, cap well above
    target, _ = tl.buy_target(100.0, mfe_level=20.0, n=999, pooled_mfe=20.0, mc_p75=200.0)
    assert target == pytest.approx(120.0)


def test_buy_target_capped_at_mc_p75():
    # projection would be 130 but the vol cone says 112 — cap wins
    target, _ = tl.buy_target(100.0, mfe_level=30.0, n=999, pooled_mfe=30.0, mc_p75=112.0)
    assert target == pytest.approx(112.0)


def test_buy_target_none_without_any_mfe():
    target, _ = tl.buy_target(100.0, mfe_level=None, n=0, pooled_mfe=None, mc_p75=None)
    assert target is None


# ---------------------------------------------------------------------------
# stop — wider/safer of ATR floor vs structural support; deepest → None
# ---------------------------------------------------------------------------

def test_buy_stop_takes_lower_of_atr_and_support():
    # entry 100, atr 4, k 2.5 → atr_stop 90; support 94 → safer (lower) is 90
    assert tl.buy_stop(100.0, support_below=94.0, atr=4.0, k_atr=2.5) == pytest.approx(90.0)


def test_buy_stop_uses_support_when_lower_than_atr():
    # atr_stop 96, support 88 → 88 is the wider/safer stop
    assert tl.buy_stop(100.0, support_below=88.0, atr=1.6, k_atr=2.5) == pytest.approx(88.0)


def test_buy_stop_none_for_deepest_rung_without_support():
    # no structural support below and no ATR → caller renders "daily close below"
    assert tl.buy_stop(100.0, support_below=None, atr=None, k_atr=2.5) is None


# ---------------------------------------------------------------------------
# regime → ATR multiplier
# ---------------------------------------------------------------------------

def test_atr_k_bull_is_tight():
    assert tl.atr_k_for_regime("bull") == tl.K_ATR_BULL


def test_atr_k_volatile_is_wide():
    assert tl.atr_k_for_regime("volatile") == tl.K_ATR_VOL


def test_atr_k_default_when_unknown():
    assert tl.atr_k_for_regime(None) == tl.K_ATR_DEFAULT


# ---------------------------------------------------------------------------
# R:R arithmetic
# ---------------------------------------------------------------------------

def test_rr_triplet():
    rr, rew, rsk = tl.rr_triplet(entry=100.0, target=120.0, stop=90.0)
    assert rew == pytest.approx(20.0)
    assert rsk == pytest.approx(10.0)
    assert rr == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# structural neighbours
# ---------------------------------------------------------------------------

def test_nearest_below_and_above():
    rungs = [80.0, 90.0, 110.0, 130.0]
    assert tl.nearest_below(100.0, rungs) == 90.0
    assert tl.nearest_above(100.0, rungs) == 110.0
    assert tl.nearest_below(80.0, rungs) is None
    assert tl.nearest_above(130.0, rungs) is None


# ---------------------------------------------------------------------------
# binary-phase field parsing
# ---------------------------------------------------------------------------

_PENDING = (
    "Binary-phase: PENDING | catalyst: Ropanicant Phase 2b topline | "
    "expected: 2026-07-06 | positive: ₹350-500 | negative-stop: ₹150\n"
)


def test_parse_binary_phase_pending_returns_anchors():
    d = tl.parse_binary_phase("intro\n" + _PENDING + "more")
    assert d is not None
    assert d["catalyst"].startswith("Ropanicant")
    assert d["positive"] == "₹350-500"
    assert d["negative-stop"] == "₹150"


def test_parse_binary_phase_completed_is_inactive():
    txt = "Binary-phase: COMPLETED (2026-07-08, positive) | catalyst: Phase 2b\n"
    assert tl.parse_binary_phase(txt) is None


def test_parse_binary_phase_absent_is_none():
    assert tl.parse_binary_phase("no marker here\n") is None


# ---------------------------------------------------------------------------
# derive_levels — orchestration
# ---------------------------------------------------------------------------

def test_buy_range_cell_uses_upper_bound_as_entry():
    stock = _stock("ACME", [_lvl("₹95-100", "BUY", 95.0, 100.0)])
    bt = {"levels": {"₹95-100": {"n": 12, "mfe_6m": 25.0}}}
    out = tl.derive_levels(stock, atr=4.0, backtest=bt, regime={"current": "bull"},
                           pooled_mfe=20.0, is_etf=False)
    buy = next(t for t in out if t.atype == "BUY")
    assert buy.entry == pytest.approx(100.0)   # UPPER bound for BUY


def test_sell_range_cell_uses_lower_bound_as_entry():
    stock = _stock("ACME", [
        _lvl("₹60", "BUY", 60.0, 60.0),
        _lvl("₹120-130", "SELL", 120.0, 130.0, signal="🚀", message="Trim 50% here"),
    ])
    out = tl.derive_levels(stock, atr=4.0, backtest={"levels": {}}, regime={}, pooled_mfe=None)
    sell = next(t for t in out if t.atype == "SELL")
    assert sell.entry == pytest.approx(120.0)   # LOWER bound for SELL
    assert sell.fmt == "trim"
    assert "Reload: ₹60" in sell.clause          # reload = nearest BUY rung below
    assert sell.clause.startswith("TRADE: Trim")  # SELL keeps trim/reload phrasing


def test_numeric_swing_buy_emits_target_stop_rr():
    # Single (deepest) BUY rung → no lower support, so the ATR floor is the stop.
    stock = _stock("ACME", [
        _lvl("₹100", "BUY", 100.0, 100.0),
        _lvl("₹150", "SELL", 150.0, 150.0, signal="🚀"),
    ])
    bt = {"levels": {"₹100": {"n": 12, "mfe_6m": 20.0}}}
    out = tl.derive_levels(stock, atr=4.0, backtest=bt, regime={"current": "bull"},
                           pooled_mfe=20.0, is_etf=False)
    buy = next(t for t in out if t.price_str == "₹100")
    assert buy.fmt == "swing"
    assert buy.target == pytest.approx(120.0)    # 100 * (1 + 20/100)
    assert buy.stop == pytest.approx(90.0)        # ATR floor: 100 − 2.5·4 = 90 (no lower support)
    assert buy.rr == pytest.approx(2.0)
    assert "TRADE: Buy at ₹100" in buy.clause
    assert "Target: ₹120" in buy.clause
    assert "SL: ₹90" in buy.clause
    assert "R:R 2" in buy.clause                  # metrics retained in the advisory tail


def test_buy_without_numeric_stop_gets_no_trade_line():
    # No ATR and no lower support → no numeric R:R → not a clean swing → no TRADE: line
    # (the level stays a pure long-term investment add; its body carries conviction).
    stock = _stock("ACME", [_lvl("₹100", "BUY", 100.0, 100.0),
                            _lvl("₹150", "SELL", 150.0, 150.0, signal="🚀")])
    bt = {"levels": {"₹100": {"n": 12, "mfe_6m": 20.0}}}
    out = tl.derive_levels(stock, atr=None, backtest=bt, regime={}, pooled_mfe=20.0)
    buy = next(t for t in out if t.price_str == "₹100")
    assert buy.stop is None
    assert buy.clause == ""


def test_weak_swing_buy_gets_no_trade_line():
    # Low MFE (5%) but a wide ATR stop → R:R well under floor → weak swing → NO TRADE: line.
    stock = _stock("ACME", [
        _lvl("₹100", "BUY", 100.0, 100.0),
        _lvl("₹105", "SELL", 105.0, 105.0, signal="🚀"),
    ])
    # target 105; stop = ATR floor 100 − 2.5·4 = 90 → R:R = 5/10 = 0.5
    bt = {"levels": {"₹100": {"n": 12, "mfe_6m": 5.0}}}
    out = tl.derive_levels(stock, atr=4.0, backtest=bt, regime={"current": "bull"}, pooled_mfe=5.0)
    buy = next(t for t in out if t.price_str == "₹100")
    assert buy.rr is not None and buy.rr < tl.RR_FLOOR   # computed, but...
    assert buy.clause == ""                              # ...no TRADE: line emitted
    assert "wait" not in buy.clause.lower()              # never a "wait" hedge


def test_etf_buy_now_carries_stop_and_metrics():
    # ETFs used to get a band line with no stop. Now they get a real SL (buy_stop)
    # and the full Buy/Target/SL + metrics + note format, like single-name swings.
    stock = _stock("ITBEES", [
        _lvl("₹31", "BUY", 31.0, 31.0),
        _lvl("₹33", "BUY", 33.0, 33.0),
        _lvl("₹39", "SELL", 39.0, 39.0, signal="⬆️"),
    ])
    bt = {"levels": {"₹33": {"n": 30, "mfe_6m": 15.0}}}
    out = tl.derive_levels(stock, atr=1.0, backtest=bt, regime={}, pooled_mfe=15.0, is_etf=True)
    buy = next(t for t in out if t.price_str == "₹33")
    assert buy.fmt == "etf_band"
    assert buy.stop is not None and buy.rr is not None      # ETF now has a stop + R:R
    assert "TRADE: Buy at ₹33" in buy.clause
    assert "Target: ₹39" in buy.clause
    assert "SL: ₹" in buy.clause
    assert "R:R" in buy.clause


def test_etf_sell_uses_trim_reload_format():
    stock = _stock("ITBEES", [
        _lvl("₹33", "BUY", 33.0, 33.0),
        _lvl("₹39-40", "SELL", 39.0, 40.0, signal="⬆️", message="Trim 25% here"),
    ])
    out = tl.derive_levels(stock, atr=1.0, backtest={"levels": {}}, regime={}, is_etf=True)
    sell = next(t for t in out if t.atype == "SELL")
    assert sell.clause.startswith("TRADE: Trim")
    assert "Reload: ₹33" in sell.clause


def test_binary_pending_routes_all_to_scenario():
    stock = _stock("SUVEN", [
        _lvl("₹200", "BUY", 200.0, 200.0),
        _lvl("₹280", "SELL", 280.0, 280.0, signal="🚀"),
    ], core_pct=30)
    binary = {
        "catalyst": "Ropanicant Phase 2b topline",
        "positive": "₹350-500",
        "negative-stop": "₹150",
    }
    bt = {"levels": {"₹200": {"n": 12, "mfe_6m": 40.0}}}
    out = tl.derive_levels(stock, atr=8.0, backtest=bt, regime={"current": "bull"},
                           pooled_mfe=30.0, binary=binary)
    buy = next(t for t in out if t.atype == "BUY")
    assert buy.fmt == "scenario"
    assert buy.rr is None                       # no fake R:R on a bimodal payoff
    assert "Ropanicant Phase 2b" in buy.clause
    assert "₹350-500" in buy.clause
    assert "₹150" in buy.clause
    # a SELL rung under a binary phase must NOT say "Accumulate"
    sell = next(t for t in out if t.atype == "SELL")
    assert sell.fmt == "scenario"
    assert "Accumulate" not in sell.clause
    assert "trim into a positive re-rate" in sell.clause


def test_regime_blocks_overlay_buy_hostile_in_bear_and_volatile():
    assert tl.regime_blocks_overlay("bear", "BUY") is True
    assert tl.regime_blocks_overlay("volatile", "BUY") is True
    assert tl.regime_blocks_overlay("bull", "BUY") is False
    assert tl.regime_blocks_overlay("sideways", "BUY") is False


def test_regime_blocks_overlay_sell_hostile_in_bull():
    assert tl.regime_blocks_overlay("bull", "SELL") is True
    assert tl.regime_blocks_overlay("bear", "SELL") is False


def test_regime_blocks_overlay_unknown_never_blocks():
    assert tl.regime_blocks_overlay("", "BUY") is False
    assert tl.regime_blocks_overlay(None, "SELL") is False


def _tlvl(price_str, atype, clause):
    return tl.TradeLevel(price_str=price_str, entry=100.0, atype=atype, fmt="swing",
                         target=120.0, stop=90.0, rr=2.0, clause=clause)


def test_select_overlay_returns_clean_swing_in_friendly_regime():
    derived = [_tlvl("₹100", "BUY", "TRADE: Buy ₹100 …")]
    got = tl.select_overlay(derived, "₹100", "bull")
    assert got is not None and got.price_str == "₹100"


def test_select_overlay_none_in_hostile_regime():
    derived = [_tlvl("₹100", "BUY", "TRADE: Buy ₹100 …")]
    assert tl.select_overlay(derived, "₹100", "bear") is None


def test_select_overlay_none_for_weak_swing_empty_clause():
    derived = [_tlvl("₹100", "BUY", "")]          # weak swing → no clause
    assert tl.select_overlay(derived, "₹100", "bull") is None


def test_binary_post_data_rung_forbids_pre_data_entry():
    # A BUY rung the file marks POST-POSITIVE-DATA-only must NOT say "Accumulate now".
    stock = _stock("SUVEN", [
        _lvl("₹229-232", "BUY", 229.0, 232.0,
             message="POST-POSITIVE-DATA reload zone ONLY. On positive: add; on negative: crash."),
    ], core_pct=30)
    binary = {"catalyst": "Phase 2b", "positive": "₹350-500", "negative-stop": "₹150"}
    out = tl.derive_levels(stock, atr=8.0, backtest={"levels": {}}, regime={}, binary=binary)
    buy = out[0]
    assert buy.fmt == "scenario"
    assert "Accumulate" not in buy.clause
    assert "POST-POSITIVE-DATA reload only" in buy.clause


# ---------------------------------------------------------------------------
# Sable note: format, threading, and round-trip preservation
# ---------------------------------------------------------------------------

def test_authored_note_appears_as_final_segment():
    stock = _stock("ACME", [
        _lvl("₹100", "BUY", 100.0, 100.0),
        _lvl("₹150", "SELL", 150.0, 150.0, signal="🚀"),
    ])
    bt = {"levels": {"₹100": {"n": 12, "mfe_6m": 20.0}}}
    out = tl.derive_levels(stock, atr=4.0, backtest=bt, regime={"current": "bull"},
                           pooled_mfe=20.0, comments={"₹100": "Fear is peaking — back the truck up."})
    buy = next(t for t in out if t.price_str == "₹100")
    assert buy.clause.endswith("Fear is peaking — back the truck up.")
    assert " │ " in buy.clause                       # uses the look-alike separator
    assert "|" not in buy.clause                      # never a raw ASCII pipe (table-safe)


def test_unannotated_level_gets_fallback_note():
    stock = _stock("ACME", [
        _lvl("₹100", "BUY", 100.0, 100.0),
        _lvl("₹150", "SELL", 150.0, 150.0, signal="🚀"),
    ])
    bt = {"levels": {"₹100": {"n": 12, "mfe_6m": 20.0}}}
    out = tl.derive_levels(stock, atr=4.0, backtest=bt, regime={"current": "bull"}, pooled_mfe=20.0)
    buy = next(t for t in out if t.price_str == "₹100")
    assert buy.clause.endswith(tl._fallback_comment(buy, "bull"))   # deterministic seed note


def test_parse_existing_comments_round_trips_authored_note():
    # Render a clause with an authored note, embed it in a table row, parse it back.
    note = "Deep value, fear peaking; accumulate without hesitation."
    stock = _stock("ITBEES", [
        _lvl("₹33", "BUY", 33.0, 33.0),
        _lvl("₹39", "SELL", 39.0, 39.0, signal="⬆️"),
    ])
    out = tl.derive_levels(stock, atr=1.0, backtest={"levels": {}}, regime={},
                           is_etf=True, comments={"₹33": note})
    buy = next(t for t in out if t.price_str == "₹33")
    md = f'| 🟢 | ₹33 | BUY | "ITBEES at ₹33 — Add. {buy.clause}" |\n'
    parsed = tl.parse_existing_comments(md)
    assert parsed.get("₹33") == note


def test_parse_existing_comments_ignores_metric_only_tail():
    # A line whose final segment is a numeric metric (no authored note) yields no entry.
    md = '| 🟢 | ₹33 | BUY | "x TRADE: Buy at ₹33 │ Target: ₹39 (+18%) │ SL: ₹30 │ R:R 3" |\n'
    assert tl.parse_existing_comments(md) == {}


def test_parse_existing_comments_keeps_note_starting_with_metric_word():
    # A real note may begin with "R:R"/"Target"/etc. — it must NOT be mistaken for a
    # metric segment (regression: a note starting "R:R thins out…" was being dropped).
    md = ('| 🟢 | ₹33 | BUY | "x TRADE: Buy at ₹33 │ Target: ₹39 │ SL: ₹30 │ '
          'R:R 3, risk −9% │ R:R thins out up here — top-up only." |\n')
    assert tl.parse_existing_comments(md) == {"₹33": "R:R thins out up here — top-up only."}
