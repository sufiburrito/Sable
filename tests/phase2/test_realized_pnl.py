"""
Trade Journal — Phase B: FIFO realized-P&L engine (gross).

Pins the lot-matching that becomes the broker-reconcilable closed_lots table:
FIFO order, partial fills, STCG/LTCG split, and quarantine of corp-actions / oversells.
"""
from datetime import date

from journal.realized_pnl import compute_closed_lots


def _t(tt, qty, price, d, sym="X", is_ca=False):
    return {"symbol": sym, "trade_type": tt, "quantity": qty, "price": price,
            "date": d, "is_ca": is_ca}


def test_simple_round_trip():
    closed, openl, q = compute_closed_lots([
        _t("BUY", 10, 100.0, date(2025, 1, 1)),
        _t("SELL", 10, 120.0, date(2025, 2, 1)),
    ])
    assert len(closed) == 1 and not openl and not q
    c = closed[0]
    assert c["realized_pnl"] == 200.0 and c["realized_pct"] == 20.0
    assert c["gain_type"] == "STCG" and c["quantity"] == 10


def test_partial_sell_leaves_open_lot():
    closed, openl, _ = compute_closed_lots([
        _t("BUY", 10, 100.0, date(2025, 1, 1)),
        _t("SELL", 4, 120.0, date(2025, 2, 1)),
    ])
    assert closed[0]["quantity"] == 4 and closed[0]["realized_pnl"] == 80.0
    assert len(openl) == 1 and openl[0]["quantity"] == 6


def test_fifo_consumes_earliest_lots_first():
    closed, openl, _ = compute_closed_lots([
        _t("BUY", 5, 100.0, date(2025, 1, 1)),
        _t("BUY", 5, 110.0, date(2025, 1, 2)),
        _t("SELL", 7, 120.0, date(2025, 3, 1)),
    ])
    # 5 from the ₹100 lot (+₹100), 2 from the ₹110 lot (+₹20); 3 of the ₹110 lot left open
    assert [c["quantity"] for c in closed] == [5, 2]
    assert [c["realized_pnl"] for c in closed] == [100.0, 20.0]
    assert openl[0]["quantity"] == 3 and openl[0]["buy_price"] == 110.0


def test_ltcg_when_held_over_a_year():
    closed, *_ = compute_closed_lots([
        _t("BUY", 1, 100.0, date(2024, 1, 1)),
        _t("SELL", 1, 150.0, date(2025, 6, 1)),     # ~17 months
    ])
    assert closed[0]["gain_type"] == "LTCG"


def test_oversell_is_quarantined_not_miscomputed():
    closed, openl, q = compute_closed_lots([
        _t("SELL", 10, 120.0, date(2025, 2, 1)),    # no matching buy on record
    ])
    assert not closed and not openl
    assert len(q) == 1 and q[0]["reason"] == "sell_without_matching_buy"
    assert q[0]["unmatched_qty"] == 10


def test_corporate_action_and_null_price_quarantined():
    closed, _, q = compute_closed_lots([
        _t("SELL", 2, None, date(2025, 2, 1), sym="SUZLON-RE", is_ca=True),
        _t("BUY", 5, None, date(2025, 1, 1)),       # missing price
    ])
    assert not closed and len(q) == 2
