"""
report_generator level-text normalisation.

Guards the per-character explosion bug: key_support/key_resistance were authored
as a *string* in some report JSONs, and iterating a string yields one character
at a time → one bullet per char. _as_levels / _levels_read_prose must always
produce whole zones / readable prose, never per-character fragments.
"""
from report_generator import _as_levels, _levels_read_prose, _signal_html


def test_as_levels_passes_lists_through():
    items = ["₹674-685 — triple MA confluence", "₹633-660 — 11-touch fortress"]
    assert _as_levels(items) == items


def test_as_levels_splits_string_into_zones_not_characters():
    s = ("₹1,029-1,034 (2-touch; June 16 false break held), "
         "₹974-1,003 (6-touch, 50d MA ₹1,021; 12 entries, 50% win), "
         "₹921-962 (8-touch, 200d+50w MA)")
    out = _as_levels(s)
    # Three zones, each whole — NOT 100+ single-character fragments.
    assert len(out) == 3
    assert out[0].startswith("₹1,029-1,034")
    assert out[1].startswith("₹974-1,003")
    # Internal thousands-comma (₹1,021) must stay inside its zone, not split it.
    assert "₹1,021" in out[1]
    assert all(len(z) > 2 for z in out)   # no per-character fragments


def test_as_levels_period_separated_resistance():
    s = "₹1,092-1,135 (8-touch, 52W high; confirmed ceiling). ₹1,243. ₹1,385 (ATH)."
    out = _as_levels(s)
    assert len(out) == 3
    assert out[1] == "₹1,243"
    assert out[2].startswith("₹1,385")


def test_as_levels_no_rupee_boundary_stays_one_item():
    assert _as_levels("a single freeform note about levels") == [
        "a single freeform note about levels"]


def test_as_levels_empty():
    assert _as_levels(None) == []
    assert _as_levels("") == []
    assert _as_levels([]) == []


def test_levels_read_prefers_explicit_field():
    tech = {"levels_read": "₹674 is the entry; ₹871 caps it.",
            "key_support": ["ignored"], "key_resistance": ["ignored"]}
    assert _levels_read_prose(tech) == "₹674 is the entry; ₹871 caps it."


def test_levels_read_synthesises_from_legacy_string_without_explosion():
    tech = {"key_support": "₹1,029-1,034 (held), ₹974-1,003 (6-touch)",
            "key_resistance": "₹1,243. ₹1,385 (ATH)"}
    prose = _levels_read_prose(tech)
    assert "<b>Support:</b>" in prose and "<b>Resistance:</b>" in prose
    assert "₹1,029-1,034" in prose and "₹1,385" in prose
    # The smoking gun: no "<li>₹</li><li>1</li>..." — prose has no <li> at all.
    assert "<li>" not in prose


def test_levels_read_from_legacy_list():
    tech = {"key_support": ["₹674-685 — MA confluence"], "key_resistance": []}
    prose = _levels_read_prose(tech)
    assert prose == "<b>Support:</b> ₹674-685 — MA confluence"


def test_levels_read_empty_when_nothing():
    assert _levels_read_prose({}) == ""


# ── Signal column: emoji → CSS dot, with resilient fallbacks ────────────────

def test_signal_html_exact_emoji_maps_to_dot():
    out = _signal_html("🟢", "BUY")
    assert "<span" in out and "#16a34a" in out and "🟢" not in out


def test_signal_html_extracts_embedded_emoji():
    # "BUY 🟡" is not an exact key, but the emoji must still be found.
    assert _signal_html("BUY 🟡", "BUY") == _signal_html("🟡", "BUY")
    # Longest match wins: 🚀🚀 over a single 🚀.
    assert "↑↑↑↑" in _signal_html("SELL 🚀🚀", "SELL")


def test_signal_html_text_falls_back_to_type_dot():
    # Regression: text signals → a colored dot from the row type, never raw words.
    assert _signal_html("Strong BUY", "BUY") == _signal_html("🟢", "BUY")  # green ●
    assert "◎" in _signal_html("Watch", "WATCH")
    assert "↑" in _signal_html("Hard SELL", "SELL")
    # The literal words must not survive into the cell.
    assert "Strong BUY" not in _signal_html("Strong BUY", "BUY")


def test_signal_html_unknown_with_no_type_returns_raw():
    assert _signal_html("???", "") == "???"
    assert _signal_html("", "") == ""
