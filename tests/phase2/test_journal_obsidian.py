"""
Trade Journal — Phase C (Obsidian vault writer).

Pins the safety-critical bits: frontmatter emit, stable note ids, create-if-absent
(reflections never clobbered), radar metric ranges, and dashboard composition.
"""
import journal.obsidian as ob


def test_frontmatter_quotes_strings_bare_numbers():
    fm = ob._frontmatter({"symbol": "MANGIND$", "quantity": 5, "price": 8.86})
    assert 'symbol: "MANGIND$"' in fm           # string quoted (special char safe)
    assert "quantity: 5" in fm and "price: 8.86" in fm   # numbers bare
    assert fm.startswith("---\n") and fm.rstrip().endswith("---")


def test_tid_is_stable_and_short():
    a = ob._tid("BBOX", "2024-01-01", "2025-01-01", 14, 1090.3)
    b = ob._tid("BBOX", "2024-01-01", "2025-01-01", 14, 1090.3)
    assert a == b and len(a) == 6
    assert a != ob._tid("BBOX", "2024-01-01", "2025-01-01", 14, 1090.4)


def test_write_if_absent_creates_then_preserves(tmp_path):
    p = tmp_path / "note.md"
    assert ob._write_if_absent(p, "original") is True
    assert ob._write_if_absent(p, "REPLACEMENT") is False   # exists → not rewritten
    assert p.read_text() == "original"                      # reflection preserved


def test_refresh_note_updates_frontmatter_keeps_body(tmp_path):
    p = tmp_path / "m.md"
    v1 = '---\ntype: "missed"\ncorroboration: "pending"\n---\n# Head\n\n## Lesson\n'
    assert ob._refresh_note(p, v1) == "created"
    # the user writes a reflection into the body
    p.write_text(p.read_text() + "I skipped it because the regime was bearish.\n")
    v2 = '---\ntype: "missed"\ncorroboration: "target_hit"\n---\n# Head\n\n## Lesson\n'
    assert ob._refresh_note(p, v2) == "updated"
    out = p.read_text()
    assert 'corroboration: "target_hit"' in out               # managed frontmatter refreshed
    assert "I skipped it because the regime was bearish." in out   # user body preserved
    assert ob._refresh_note(p, v2) == "kept"                  # idempotent — no churn


def test_verified_exit_quality_columns_present():
    # the Execution Review view exposes the verified column; the Missed table carries corroboration
    recs = [{"ticker": "X", "tier": "on_level", "advised_entry": 100.0, "advised_target": 120.0,
             "user_buy_price": 99.0, "entry_slippage_pct": -1.0, "lag_days": 0,
             "user_sell_price": 110.0, "exit_vs_target_pct": -8.3, "realized_pnl": 110.0,
             "left_on_table_pct": 2.0, "exit_quality": "pending", "status": "taken_closed",
             "fired_on": "2026-01-10"}]
    md = ob.build_execution_view(recs)
    assert "Left on table" in md and '"quality": "pending"' in md   # verified-exit data injected
    assert "leftCell" in ob._JS_EXEC_TABLE                          # JS formats the verdict
    assert "corroboration" in ob._JS_MISSED_TABLE and "Actual peak %" in ob._JS_MISSED_TABLE


def test_trade_note_carries_frontmatter_and_reflection():
    lot = {"symbol": "X", "quantity": 3, "buy_date": "2025-01-01", "buy_price": 100.0,
           "sell_date": "2025-02-01", "sell_price": 120.0, "realized_pnl": 60.0,
           "realized_pct": 20.0, "holding_days": 31, "gain_type": "STCG"}
    path, content = ob.trade_note(lot)
    assert path.name.endswith(".md") and "Trades" in str(path)
    assert 'type: "trade"' in content and "## Lesson" in content


def test_radar_metrics_in_range():
    closed = [{"realized_pnl": 100.0, "sell_date": "2025-01-10"},
              {"realized_pnl": -40.0, "sell_date": "2025-02-10"},
              {"realized_pnl": 60.0, "sell_date": "2025-03-10"}]
    ledger = [{"alert_type": "BUY", "entry": 100.0, "status": "win",
               "fired_at": "2025-01-01T09:00:00+05:30", "ticker": "Z"}]
    vals = ob._radar_metrics(closed, ledger)
    assert len(vals) == 5 and all(0 <= v <= 100 for v in vals)


def test_dataview_sources_are_vault_root_relative():
    # The vault root IS journal/vault/ when opened in Obsidian, so Dataview sources
    # must be "Trades"/"Missed" — never "journal/vault/Trades" (would match nothing).
    for blob in (ob._TRADES_DB, ob._MISSED_DB, ob._JS_KPI, ob._JS_CAL,
                 ob._JS_MISSED_KPI, ob._JS_MISSED_TABLE):
        assert "journal/vault" not in blob
    assert 'FROM "Trades"' in ob._TRADES_DB
    assert '"Trades"' in ob._JS_KPI and '"Trades"' in ob._JS_CAL
    assert '"Missed"' in ob._JS_MISSED_KPI and '"Missed"' in ob._JS_MISSED_TABLE


def test_missed_db_has_dashboard_and_sortable_table():
    md = ob._MISSED_DB
    assert "## Summary" in md and md.count("```dataviewjs") == 2     # KPI block + table block
    assert 'class="sh"' in md and "sortKey" in md                   # clickable sortable headers
    assert "openLinkText" in md                                     # ticker → note link


def test_build_analytics_has_all_three_sections():
    md = ob.build_analytics(
        [{"realized_pnl": 100.0, "sell_date": "2025-01-10"}], [])
    assert "## Scorecard" in md and "## P&L calendar" in md
    assert "## Performance profile" in md and "<svg" in md   # radar = inline SVG (no Charts plugin)
    assert md.count("```dataviewjs") == 3        # KPI + calendar + radar, all DataviewJS
    assert "## Recent reviews" in md and 'FROM "Reviews"' in md   # C4 reviews list


def test_scorecard_kpis_are_timeframe_scoped():
    md = ob.build_analytics([{"realized_pnl": 100.0, "sell_date": "2025-01-10"}], [])
    # the toggle: Week/Month/FY/All, default Month, with click handlers + a Sable read
    for tf in ("Week", "Month", "FY", "All"):
        assert f'"{tf}"' in ob._JS_KPI
    assert 'let active = "Month"' in ob._JS_KPI and "data-tf=" in ob._JS_KPI
    assert "Sable:" in ob._JS_KPI                        # live commentary follows the toggle
    assert "all-time" in md and "[[Effective P&L]]" in md  # radar caveat + gross→net cross-link


def test_scorecard_has_toggle_following_bar_chart():
    # the windowed P&L bars redraw on each toggle via the Charts plugin, with a graceful fallback
    assert "drawChart(active)" in ob._JS_KPI and "window.renderChart" in ob._JS_KPI
    assert "Enable the Charts plugin" in ob._JS_KPI      # fallback when plugin absent


def test_equity_curve_block_is_cumulative_monthly():
    closed = [
        {"sell_date": "2025-01-10", "realized_pnl": 100.0},
        {"sell_date": "2025-01-20", "realized_pnl": 50.0},     # same month → bucketed
        {"sell_date": "2025-03-05", "realized_pnl": -40.0},
    ]
    block = ob._equity_curve_block(closed)
    assert block.startswith("```chart") and "Cumulative P&L" in block
    assert '"Jan 2025"' in block and '"Mar 2025"' in block     # one label per month traded
    assert "[150, 110]" in block                               # cumulative: 100+50=150, then −40→110


def test_equity_curve_block_handles_empty():
    assert "once you've booked a trade" in ob._equity_curve_block([])


def test_build_analytics_embeds_equity_curve():
    md = ob.build_analytics([{"realized_pnl": 100.0, "sell_date": "2025-01-10"}], [])
    assert "## Equity curve" in md and "```chart" in md


def test_review_template_seeded_structure():
    assert "type: review" in ob._REVIEW_TEMPLATE
    assert "## Plan for tomorrow" in ob._REVIEW_TEMPLATE and "Duplicate this note" in ob._REVIEW_TEMPLATE


def test_journal_build_stage_isolation():
    import journal.build as jb
    assert jb._stage("ok", lambda: None) is True

    def boom():
        raise RuntimeError("kaboom")
    assert jb._stage("bad", boom) is False     # a raising stage is caught, not propagated
