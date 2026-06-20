"""
Hot-path test: the labeled, scannable alert layout (bean mmo8) + regime-gated
tactical overlay (bean 96ic).

Pins the structure of _compose_alert_body:
  {emoji} {TIER} · <b>ACTION TICKER</b> @ ₹price
  📍 regime   /   💭 thesis   /   🎯 sizing[ → tactical]
  📈 dma (optional)   /   📊 position · stats (merged)   /   <i>Sable …</i> (optional)
and that the alert ALWAYS fires (with regime visible) even when the overlay is
suppressed or a helper crashes.
"""
from types import SimpleNamespace

import alert_bot.main as main


def _alert(ticker="HBLENGINE", alert_type="BUY", price_str="₹613", message="Range dip."):
    return SimpleNamespace(ticker=ticker, alert_type=alert_type, price_str=price_str,
                           message=message)


def _overlay(atype="BUY", target=200.0, stop=160.0, rr=2.5, reload_to=None):
    return SimpleNamespace(atype=atype, target=target, stop=stop, rr=rr, reload_to=reload_to)


def _patch(monkeypatch, *, overlay=None, verdict="HIGH CONVICTION", emoji="🟢",
           thesis="Adding here extends your swing layer.", dma=None,
           position=None, stats=None, sable=None):
    conf = SimpleNamespace(verdict=verdict, emoji=emoji, thesis=thesis, sable_opinion=sable)
    monkeypatch.setattr(main, "compute_confidence", lambda *a, **k: conf)
    monkeypatch.setattr(main, "_regime_header", lambda *a, **k: "Bull 85% →")
    monkeypatch.setattr(main, "live_overlay", lambda *a, **k: overlay)
    monkeypatch.setattr(main, "dma_hint", lambda *a, **k: dma)
    monkeypatch.setattr(main, "portfolio_fragment", lambda *a, **k: position)
    monkeypatch.setattr(main, "format_stats_line", lambda *a, **k: stats)


def _compose(alert=None, price=613.0):
    state = SimpleNamespace(mmi_last_value=None)
    body, _ = main._compose_alert_body(alert or _alert(), price, {}, state, {})
    return body.split("\n")


def test_header_has_emoji_tier_and_bold(monkeypatch):
    _patch(monkeypatch)
    lines = _compose()
    assert lines[0] == "🟢 HIGH · <b>BUY HBLENGINE</b> @ ₹613"
    assert lines[1] == "📍 Bull 85% →"
    assert lines[2] == "💭 Adding here extends your swing layer."
    assert lines[3].startswith("🎯 ")


def test_overlay_present_shows_tactical_tail(monkeypatch):
    _patch(monkeypatch, overlay=_overlay(target=200.0, stop=160.0, rr=2.5))
    trade = next(l for l in _compose() if l.startswith("🎯"))
    assert "→ ₹200" in trade and "🛑 ₹160" in trade and "R:R 2.5" in trade


def test_overlay_suppressed_no_tactical_but_fires(monkeypatch):
    # hostile regime / weak swing → tactical tail withheld, but the alert still fires
    _patch(monkeypatch, overlay=None, emoji="🔴", verdict="WEAK")
    lines = _compose()
    assert lines[0].startswith("🔴 WEAK · <b>BUY HBLENGINE</b>")
    assert lines[1] == "📍 Bull 85% →"             # regime stays visible
    trade = next(l for l in lines if l.startswith("🎯"))
    assert "→" not in trade and "R:R" not in trade  # no tactical numbers


def test_sell_overlay_shows_reload(monkeypatch):
    _patch(monkeypatch, overlay=_overlay(atype="SELL", reload_to=150.0),
           verdict="STRONG SELL", emoji="🟢")
    trade = next(l for l in _compose(_alert(alert_type="SELL")) if l.startswith("🎯"))
    assert "→ reload ₹150" in trade


def test_context_merges_position_and_stats(monkeypatch):
    _patch(monkeypatch, position="Holding 40 @ ₹676 (−9%)",
           stats="5/13 signals agree · VCP 51 (fine to add, no rush)")
    ctx = next(l for l in _compose() if l.startswith("📊"))
    assert ctx == "📊 Holding 40 @ ₹676 (−9%) · 5/13 signals agree · VCP 51 (fine to add, no rush)"


def test_no_context_line_when_nothing(monkeypatch):
    _patch(monkeypatch, position=None, stats=None)
    assert not any(l.startswith("📊") for l in _compose())


def test_optional_dma_and_sable_lines(monkeypatch):
    _patch(monkeypatch, dma="₹613 hugs a rising 200-DMA", sable="I like this entry.")
    lines = _compose()
    assert any(l == "📈 ₹613 hugs a rising 200-DMA" for l in lines)
    assert lines[-1] == "<i>Sable — I like this entry.</i>"


def test_survives_overlay_crash_and_still_fires(monkeypatch):
    _patch(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("overlay blew up")
    monkeypatch.setattr(main, "live_overlay", boom)

    lines = _compose()
    assert lines[0].startswith("🟢 HIGH · <b>BUY HBLENGINE</b>")   # alert still composed
    assert next(l for l in lines if l.startswith("🎯"))            # sizing-only trade line
