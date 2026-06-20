"""
Trade Journal — Tax Planning: Indian CG set-off, exemption, LTCG-threshold, key dates.

Pure functions only (no OHLC/network). Planning aid, not tax advice.
"""
import datetime as dt

from journal import tax

MODEL = {"stcg_rate": 0.20, "ltcg_rate": 0.125, "ltcg_exemption": 125000}


# ── financial year ───────────────────────────────────────────────────────────

def test_india_fy_runs_april_to_march():
    label, start, end = tax.india_fy(dt.date(2026, 6, 18))
    assert label == "FY2026-27"
    assert start == dt.date(2026, 4, 1) and end == dt.date(2027, 3, 31)
    # a January date belongs to the FY that started the previous April
    assert tax.india_fy(dt.date(2027, 1, 5))[0] == "FY2026-27"


# ── set-off rules ────────────────────────────────────────────────────────────

def test_stcl_offsets_both_stcg_and_ltcg():
    # STCL 30k wipes STCG 20k, the 10k remainder spills into LTCG
    r = {"stcg": 20000, "ltcg": 50000, "stcl": 30000, "ltcl": 0}
    t = tax.compute_tax(r, MODEL)
    assert t["net_stcg"] == 0
    assert t["net_ltcg"] == 40000          # 50k − 10k spillover


def test_ltcl_offsets_only_ltcg_not_stcg():
    r = {"stcg": 20000, "ltcg": 5000, "stcl": 0, "ltcl": 30000}
    t = tax.compute_tax(r, MODEL)
    assert t["net_stcg"] == 20000          # LTCL cannot touch STCG
    assert t["net_ltcg"] == 0              # 5k − 30k floored at 0


def test_ltcg_exemption_applied_and_tax_is_rate_times_net():
    r = {"stcg": 100000, "ltcg": 200000, "stcl": 0, "ltcl": 0}
    t = tax.compute_tax(r, MODEL)
    assert t["taxable_ltcg"] == 75000      # 200k − 125k exemption
    assert t["exemption_used"] == 125000 and t["exemption_left"] == 0
    assert t["tax_stcg"] == 20000          # 100k × 20%
    assert t["tax_ltcg"] == 9375           # 75k × 12.5%
    assert t["total_tax"] == 29375


def test_ltcg_under_exemption_is_untaxed_with_headroom_left():
    r = {"stcg": 0, "ltcg": 50000, "stcl": 0, "ltcl": 0}
    t = tax.compute_tax(r, MODEL)
    assert t["taxable_ltcg"] == 0 and t["tax_ltcg"] == 0
    assert t["exemption_left"] == 75000


# ── FY bucketing of realized lots ────────────────────────────────────────────

def test_fy_realized_buckets_by_sell_date_and_class():
    closed = [
        {"sell_date": "2026-05-01", "gain_type": "STCG", "realized_pnl": 1000},
        {"sell_date": "2026-05-02", "gain_type": "STCG", "realized_pnl": -400},
        {"sell_date": "2026-05-03", "gain_type": "LTCG", "realized_pnl": 2000},
        {"sell_date": "2027-04-01", "gain_type": "LTCG", "realized_pnl": 9999},  # next FY, excluded
    ]
    r = tax.fy_realized(closed, dt.date(2026, 4, 1), dt.date(2027, 3, 31))
    assert r["stcg"] == 1000 and r["stcl"] == 400 and r["ltcg"] == 2000 and r["ltcl"] == 0


# ── harvest candidates ───────────────────────────────────────────────────────

def test_harvest_candidates_tag_class_and_rank_by_offset():
    holdings = [
        {"symbol": "A", "unrealized": -1000, "gain_class": "STCG", "quantity": 1,
         "buy_price": 0, "price": 0, "holding_days": 10},
        {"symbol": "B", "unrealized": -1000, "gain_class": "LTCG", "quantity": 1,
         "buy_price": 0, "price": 0, "holding_days": 400},
        {"symbol": "C", "unrealized": 500, "gain_class": "STCG", "quantity": 1,
         "buy_price": 0, "price": 0, "holding_days": 10},   # winner, excluded
    ]
    out = tax.harvest_candidates(holdings, MODEL)
    assert [h["symbol"] for h in out] == ["A", "B"]         # winner dropped, ranked by offset
    a, b = out
    assert a["loss_class"] == "STCL" and a["max_tax_offset"] == 200   # 1000 × 20%
    assert b["loss_class"] == "LTCL" and b["max_tax_offset"] == 125   # 1000 × 12.5%


# ── carry-forward of losses ──────────────────────────────────────────────────

def test_apply_carryforward_with_no_buckets_matches_compute_tax():
    r = {"stcg": 100000, "ltcg": 200000, "stcl": 0, "ltcl": 0}
    assert tax.apply_carryforward(r, MODEL)["total_tax"] == tax.compute_tax(r, MODEL)["total_tax"]


def test_net_loss_year_carries_split_stcl_and_ltcl():
    # pure-loss FY: nothing taxed, both loss types flow out for future use
    r = {"stcg": 0, "ltcg": 0, "stcl": 40000, "ltcl": 20000}
    cf = tax.apply_carryforward(r, MODEL)
    assert cf["total_tax"] == 0
    assert cf["cf_stcl_out"] == 40000 and cf["cf_ltcl_out"] == 20000


def test_brought_forward_stcl_offsets_both_heads():
    # b/f STCL 50k wipes STCG 20k then spills onto LTCG 40k → 10k LTCG left (under exemption → tax 0)
    r = {"stcg": 20000, "ltcg": 40000, "stcl": 0, "ltcl": 0}
    cf = tax.apply_carryforward(r, MODEL, cf_stcl=50000, cf_ltcl=0)
    assert cf["net_stcg"] == 0 and cf["net_ltcg"] == 10000
    assert cf["cf_used"] == 50000 and cf["cf_stcl_out"] == 0
    assert cf["total_tax"] == 0


def test_brought_forward_ltcl_cannot_touch_stcg():
    # b/f LTCL only offsets LTCG; STCG stays fully taxable
    r = {"stcg": 30000, "ltcg": 5000, "stcl": 0, "ltcl": 0}
    cf = tax.apply_carryforward(r, MODEL, cf_stcl=0, cf_ltcl=40000)
    assert cf["net_stcg"] == 30000               # untouched by LTCL
    assert cf["net_ltcg"] == 0 and cf["cf_ltcl_out"] == 35000
    assert cf["total_tax"] == 30000 * 0.20


def test_fy_effective_series_threads_loss_into_the_next_year():
    # FY2025-26 books a 60k loss; FY2026-27's gains should be erased by carry-forward.
    closed = [
        {"sell_date": "2025-06-01", "gain_type": "STCG", "realized_pnl": -60000,
         "quantity": 1, "buy_price": 0, "sell_price": 0},
        {"sell_date": "2026-06-01", "gain_type": "STCG", "realized_pnl": 50000,
         "quantity": 1, "buy_price": 0, "sell_price": 0},
    ]
    model = {**MODEL, "charge_rate": 0.0}
    series = tax.fy_effective_series(closed, model)          # newest first
    assert [s["fy"] for s in series] == ["FY2026-27", "FY2025-26"]
    cur, prior = series
    assert prior["net_loss"] and prior["cf_out"]["stcl"] == 60000
    assert cur["cf_in"]["stcl"] == 60000                     # loss arrives from the prior FY
    assert cur["tax_standalone"]["total_tax"] == 10000       # 50k × 20% with no offset
    assert cur["tax_after_cf"]["total_tax"] == 0             # carry-forward wipes it
    assert cur["net_takehome_cf"] == 50000


# ── LTCG-threshold watch ─────────────────────────────────────────────────────

def _holding(sym, days, unrealized):
    return {"symbol": sym, "holding_days": days, "unrealized": unrealized,
            "gain_class": "LTCG" if days > tax.LTCG_DAYS else "STCG"}


def test_ltcg_watch_flags_near_term_winner_only():
    today = dt.date(2026, 6, 18)
    holdings = [
        _holding("NEAR", 340, 4000),    # 25d to LTCG, profitable → flagged
        _holding("FAR", 200, 4000),     # 165d to LTCG → outside 45d window
        _holding("LOSS", 340, -4000),   # near but underwater → not a wait-to-save case
        _holding("DONE", 400, 4000),    # already LTCG → nothing to wait for
    ]
    out = tax.ltcg_threshold_watch(holdings, MODEL, today)
    assert [w["symbol"] for w in out] == ["NEAR"]
    w = out[0]
    assert w["days_to_ltcg"] == 25
    assert w["tax_saving"] == 300                     # gain × (20% − 12.5%) rate gap


# ── key dates ────────────────────────────────────────────────────────────────

def test_key_dates_anchor_to_where_in_the_year_we_are():
    # 18 Jun 2026: we are early in FY2026-27.
    k = tax.key_dates(dt.date(2026, 6, 18))
    assert k["fy"] == "FY2026-27"
    # harvest + FY-end belong to the CURRENT FY → Mar 2027 (correct, not a bug)
    assert k["harvest_by"] == "2027-03-28"
    assert k["next_advance_tax"] == "2026-09-15"        # next 15th after 18 Jun
    # the imminent ITR is THIS July, filing the just-completed FY2025-26 — not next year
    assert k["itr_due"] == "2026-07-31" and k["itr_fy"] == "FY2025-26"
    assert k["days_to_itr"] == 43
    assert all(k[f"days_to_{x}"] >= 0 for x in ("harvest", "advance_tax", "itr"))


def test_itr_rolls_to_next_july_once_this_july_has_passed():
    # 15 Aug 2026: 31 Jul 2026 has gone, so the next ITR is 31 Jul 2027 (files FY2026-27).
    k = tax.key_dates(dt.date(2026, 8, 15))
    assert k["itr_due"] == "2027-07-31" and k["itr_fy"] == "FY2026-27"
    # advance tax: 15 Sep 2026 is the next installment
    assert k["next_advance_tax"] == "2026-09-15"


# ── reminders dedupe (no network) ────────────────────────────────────────────

def test_discord_push_is_idempotent(tmp_path, monkeypatch):
    import alert_bot.discord_webhook as wh
    from journal import tax_reminders
    posts = []
    monkeypatch.setattr(wh, "post", lambda text, *a, **k: posts.append(text))
    monkeypatch.setattr(tax_reminders, "STATE", tmp_path / "state.json")
    data = {"fy": "FY2026-27",
            "tax": {"exemption_left": 50000, "total_tax": 1234},
            "key_dates": {"days_to_harvest": 5, "harvest_by": "2027-03-28",
                          "days_to_advance_tax": None, "next_advance_tax": None,
                          "days_to_itr": None, "itr_due": "2027-07-31"}}
    first = tax_reminders.push_discord(data, window=14)
    second = tax_reminders.push_discord(data, window=14)
    assert first == ["FY2026-27:harvest"]
    assert second == []          # state persisted → not re-posted
    assert len(posts) == 1       # webhook hit exactly once


# ── the Obsidian view ────────────────────────────────────────────────────────

def test_tax_view_renders_disclaimer_and_sections():
    import journal.obsidian as ob
    d = {"fy": "FY2026-27",
         "realized": {"stcg": 100, "ltcg": 200, "stcl": 0, "ltcl": 0},
         "tax": {"net_stcg": 100, "net_ltcg": 200, "tax_stcg": 20, "tax_ltcg": 0,
                 "total_tax": 20, "exemption_used": 200, "exemption_left": 124800},
         "harvest": [{"symbol": "A", "loss_class": "STCL", "quantity": 1, "buy_price": 10,
                      "price": 8, "harvestable_loss": 2, "max_tax_offset": 0.4}],
         "ltcg_watch": [{"symbol": "B", "holding_days": 340, "days_to_ltcg": 25,
                         "ltcg_date": "2026-07-13", "unrealized": 100, "tax_saving": 7.5}],
         "key_dates": {"days_to_harvest": 280, "harvest_by": "2027-03-28",
                       "days_to_advance_tax": 89, "next_advance_tax": "2026-09-15",
                       "days_to_itr": 43, "itr_due": "2026-07-31", "itr_fy": "FY2025-26"},
         "model": MODEL, "n_holdings": 5}
    md = ob.build_tax_view(d)
    assert "not tax advice" in md
    assert "Loss-harvest candidates" in md and "LTCG-threshold watch" in md
    assert "tax-free headroom left" in md
