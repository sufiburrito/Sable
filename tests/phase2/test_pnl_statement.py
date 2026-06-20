"""
Trade Journal — Effective P&L: charge-model defaults + the gross→charges→tax→take-home math.

The gross journal must never change; this only layers costs in a separate view.
"""
from journal.pnl_statement import build_model, effective_for_lot


def test_build_model_falls_back_to_defaults_without_statement():
    m = build_model(None)
    assert m["charge_rate"] > 0
    assert m["stcg_rate"] == 0.20 and m["ltcg_rate"] == 0.125
    assert "defaults" in m["source"]


def test_effective_layers_stcg_winner():
    m = {"charge_rate": 0.001, "stcg_rate": 0.20, "ltcg_rate": 0.125}
    lot = {"quantity": 10, "buy_price": 100, "sell_price": 150,
           "realized_pnl": 500, "gain_type": "STCG"}
    e = effective_for_lot(lot, m)
    # turnover 10×250=2500 → charges 2.5 → after 497.5 → tax 20%=99.5 → take-home 398.0
    assert e["gross"] == 500 and e["charges"] == 2.5
    assert e["after_charges"] == 497.5 and e["cg_tax"] == 99.5 and e["effective"] == 398.0


def test_ltcg_is_taxed_less_than_stcg():
    m = {"charge_rate": 0.0, "stcg_rate": 0.20, "ltcg_rate": 0.125}
    base = {"quantity": 1, "buy_price": 0, "sell_price": 0, "realized_pnl": 100}
    stcg = effective_for_lot({**base, "gain_type": "STCG"}, m)
    ltcg = effective_for_lot({**base, "gain_type": "LTCG"}, m)
    assert ltcg["cg_tax"] == 12.5 and stcg["cg_tax"] == 20.0
    assert ltcg["effective"] > stcg["effective"]


def test_losses_are_not_taxed():
    m = {"charge_rate": 0.0, "stcg_rate": 0.20, "ltcg_rate": 0.125}
    e = effective_for_lot({"quantity": 1, "buy_price": 0, "sell_price": 0,
                           "realized_pnl": -100, "gain_type": "STCG"}, m)
    assert e["cg_tax"] == 0.0 and e["effective"] == -100.0


def test_effective_view_splits_by_fy_with_carryforward():
    import journal.obsidian as ob
    model = {"charge_rate": 0.0, "stcg_rate": 0.20, "ltcg_rate": 0.125,
             "ltcg_exemption": 125000, "source": "x.xlsx"}
    closed = [
        {"sell_date": "2025-06-01", "gain_type": "STCG", "realized_pnl": -60000,
         "quantity": 1, "buy_price": 0, "sell_price": 0},
        {"sell_date": "2026-06-01", "gain_type": "STCG", "realized_pnl": 50000,
         "quantity": 1, "buy_price": 0, "sell_price": 0},
    ]
    md = ob.build_effective(closed, model)
    assert "by financial year" in md and "Year-over-year" in md
    assert "## FY2026-27" in md and "## FY2025-26" in md      # one section per FY
    assert "carry-forward" in md and "not tax advice" in md
    # per-FY lot tables are live Dataview, root-relative + date-filtered
    assert 'FROM "Trades"' in md and "sell_date >= date(2026-04-01)" in md
    assert "journal/vault" not in md
