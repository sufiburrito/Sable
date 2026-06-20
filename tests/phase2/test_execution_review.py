"""
Trade Journal — Execution Review: matching Sable's BUY calls to the user's real fills.

Captures advice-vs-execution (entry slippage, days late, exit vs target), including trades
OUTSIDE the tight ±7d/±5% window. Loose-taken calls must leave the "missed" list.
"""
import datetime as dt

from journal import execution_review as er


def _call(ticker="X", fired="2026-01-10T09:30:00", entry=100.0, target=120.0, stop=92.0):
    return {"ticker": ticker, "alert_type": "BUY", "status": "open", "fired_at": fired,
            "entry": entry, "target": target, "stop": stop, "rr": 2.5, "realized_R": None}


def _buys(*pairs):
    # load_user_buys yields real date objects (not strings) — mirror that here
    return {"X": [(dt.date.fromisoformat(d), p) for d, p in pairs]}


# ── matcher tiers ────────────────────────────────────────────────────────────

def test_on_level_match_within_tight_window():
    m = er.match_call(_call(), _buys(("2026-01-12", 103.0)))   # +2d, +3% → tight
    assert m["tier"] == "on_level" and m["lag_days"] == 2


def test_loose_match_outside_window_any_price():
    m = er.match_call(_call(), _buys(("2026-02-01", 80.0)))     # +22d, −20% → loose
    assert m["tier"] == "loose" and m["lag_days"] == 22


def test_anticipatory_buy_before_the_alert_still_counts_on_level():
    # bought 2 days BEFORE the call, near the level — the old tight test counted this; keep it
    m = er.match_call(_call(), _buys(("2026-01-08", 101.0)))
    assert m["tier"] == "on_level" and m["lag_days"] == -2


def test_forward_window_boundary_45_in_46_out():
    assert er.match_call(_call(), _buys(("2026-02-24", 80.0)))["lag_days"] == 45   # exactly 45d
    assert er.match_call(_call(), _buys(("2026-02-25", 80.0))) is None             # 46d, off-level


def test_no_match_returns_none():
    assert er.match_call(_call(), _buys(("2025-12-01", 100.0))) is None            # before & far


def test_on_level_preferred_over_a_nearer_loose_buy():
    # a loose buy 2d later vs an on-level buy 6d later → on-level wins despite being farther
    m = er.match_call(_call(), _buys(("2026-01-12", 130.0), ("2026-01-16", 102.0)))
    assert m["tier"] == "on_level" and m["lag_days"] == 6


# ── exit join + record math ──────────────────────────────────────────────────

def _lot(buy_date="2026-01-12", buy_price=103.0, qty=10, sell_price=130.0,
         sell_date="2026-02-20", pnl=270.0, days=39):
    return {"symbol": "X", "buy_date": buy_date, "buy_price": buy_price, "quantity": qty,
            "sell_price": sell_price, "sell_date": sell_date, "realized_pnl": pnl, "holding_days": days}


def test_exit_join_aggregates_tranches():
    lots = [_lot(qty=10, sell_price=130.0, pnl=270.0, sell_date="2026-02-20", days=39),
            _lot(qty=10, sell_price=110.0, pnl=70.0, sell_date="2026-03-01", days=48)]
    ex = er._exit_for_buy("X", "2026-01-12", 103.0, lots)
    assert ex["status"] == "taken_closed" and ex["sold_qty"] == 20
    assert ex["user_sell_price"] == 120.0          # vwap (130+110)/2
    assert ex["user_sell_date"] == "2026-03-01" and ex["days_held"] == 48


def test_exit_open_when_no_closed_lot():
    assert er._exit_for_buy("X", "2026-01-12", 103.0, [])["status"] == "taken_open"


def test_record_has_slippage_and_exit_vs_target():
    ledger = [_call(entry=100.0, target=120.0)]
    buys = _buys(("2026-01-12", 103.0))
    closed = [_lot(buy_price=103.0, sell_price=114.0, pnl=110.0)]
    recs = er.build_execution_review(ledger, buys, closed)
    assert len(recs) == 1
    r = recs[0]
    assert r["entry_slippage_pct"] == 3.0          # (103−100)/100
    assert r["exit_vs_target_pct"] == -5.0         # sold 114 vs target 120 → −5%
    assert r["status"] == "taken_closed" and r["realized_pnl"] == 110.0


# ── missed-list reconciliation ───────────────────────────────────────────────

def test_loosely_taken_call_leaves_the_missed_list():
    from journal import missed_trades as mt
    call = _call(entry=100.0)
    call["status"] = "loss"; call["realized_R"] = -1.0   # a resolved call that'd otherwise be "missed"
    buys = _buys(("2026-02-01", 80.0))                   # loose buy +22d → counts as taken
    assert er.match_call(call, buys) is not None
    assert mt.build_missed([call], buys) == []           # excluded from missed


# ── the view ─────────────────────────────────────────────────────────────────

def test_exit_quality_verdicts():
    assert er._exit_quality(8.0, complete=True) == "early"     # really ran higher after sell
    assert er._exit_quality(0.2, complete=True) == "good"      # sold at the actual high
    assert er._exit_quality(1.5, complete=True) == "ok"
    assert er._exit_quality(8.0, complete=False) == "pending"  # too recent — don't judge
    assert er._exit_quality(None, complete=True) == "n/a"      # no OHLC after the sell


# ── the view ─────────────────────────────────────────────────────────────────

def test_execution_view_renders_sortable_table_with_entry_verification():
    import journal.obsidian as ob
    recs = [{"ticker": "X", "tier": "loose", "advised_entry": 100.0, "advised_target": 120.0,
             "entry_hit": False, "entry_closest": 104.0,
             "user_buy_price": 95.0, "entry_slippage_pct": -5.0, "lag_days": 20,
             "user_sell_price": 118.0, "exit_vs_target_pct": -1.7, "realized_pnl": 230.0,
             "left_on_table_pct": 2.0, "exit_quality": "pending",
             "status": "taken_closed", "fired_on": "2026-01-10"}]
    md = ob.build_execution_view(recs)
    assert "Execution Review" in md and "[[Missed Trades DB]]" in md
    # sortable DataviewJS table (not a static markdown table)
    assert "```dataviewjs" in md and 'class="sh"' in md and "sortKey" in md
    # the record is injected as JSON with the entry-verification fields
    assert '"entry_hit": false' in md and '"entry_closest": 104.0' in md
    assert '"tier": "loose"' in md and '"slip": -5.0' in md
    # the entry cell renders ✓ or (closest); the verified-exit cell exists
    assert "entryCell" in md and "Left on table" in md and "✓" in md
    assert ob.build_execution_view([]).find("once you've bought") > 0    # empty-state
