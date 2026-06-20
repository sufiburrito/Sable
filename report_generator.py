#!/usr/bin/env python3
"""
Generate a styled PDF analysis report for a stock ticker.

Usage:
    python3 report_generator.py <json_data_file> [output.pdf]

The JSON data file should follow the structure in reports/_schema.json.
Output defaults to reports/TICKER_YYYYMMDD.pdf.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import weasyprint

from sr_levels import compute_sr

REPORTS_DIR = Path(__file__).parent / "reports"
ANALYSIS_DIR = Path(__file__).parent / "analysis"


def _money(n) -> str:
    """Indian-style integer rupee with thousands separators (no decimals)."""
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return str(n)


def _as_levels(v) -> list[str]:
    """Normalise a key_support/key_resistance value into a list of zone strings.

    Legacy data is inconsistent: some reports store a *list* of zone strings,
    others a single comma/period-joined *string*. Iterating a string yields one
    character at a time (the per-character bullet explosion we are killing), so we
    split a string into zones at the ". ₹" / ", ₹" boundaries (each zone starts
    with ₹; the required whitespace keeps internal thousands-commas like ₹1,029
    intact). Falsy → [].
    """
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    parts = re.split(r"[.,]\s+(?=₹)", str(v).strip())
    return [p.strip().rstrip(".") for p in parts if p.strip()]


def _levels_read_prose(tech: dict) -> str:
    """The advisor's prose read on levels for the Historical S/R card.

    Prefers the explicit `levels_read` field (the going-forward shape). For older
    reports that only carry key_support/key_resistance, synthesise a prose line
    from them — preserving their content while never iterating a string into
    per-character bullets.
    """
    explicit = tech.get("levels_read")
    if explicit:
        return str(explicit)
    sup = [z.rstrip(". ") for z in _as_levels(tech.get("key_support"))]
    res = [z.rstrip(". ") for z in _as_levels(tech.get("key_resistance"))]
    parts = []
    if sup:
        parts.append("<b>Support:</b> " + "; ".join(sup))
    if res:
        parts.append("<b>Resistance:</b> " + "; ".join(res))
    return " &nbsp; ".join(parts)


def _sr_zone_rows(zones: list[dict], kind: str) -> str:
    """Rows for the touch-tested S/R table. kind ∈ {'support','resistance'}."""
    color = "#16a34a" if kind == "support" else "#dc2626"
    label = "Support" if kind == "support" else "Resistance"
    rows = ""
    for z in zones:
        band = (f"₹{_money(z['low'])}–{_money(z['high'])}"
                if z["low"] != z["high"] else f"₹{_money(z['price'])}")
        rows += (
            "<tr>"
            f"<td>{band}</td>"
            f"<td style='text-align:center'><b>{z['touches']}</b></td>"
            f"<td style='text-align:center;color:#6b7280'>{z.get('latest_date','')}</td>"
            f"<td style='color:{color};font-weight:700'>{label}</td>"
            "</tr>"
        )
    return rows


def _fib_rows(fib: list[dict]) -> str:
    """Rows for the Fibonacci retracement table, confluence badged."""
    rows = ""
    for f in fib:
        color = "#16a34a" if f["type"] == "support" else "#dc2626"
        label = "Support" if f["type"] == "support" else "Resistance"
        pct = f"{f['ratio'] * 100:.1f}".rstrip("0").rstrip(".") + "%"
        name = pct
        if f.get("confluence"):
            name += (f" <span class='fib-confl'>★ also a {f['confluence']}-touch "
                     f"zone</span>")
        rows += (
            "<tr>"
            f"<td>{name}</td>"
            f"<td style='text-align:center'><b>₹{_money(f['price'])}</b></td>"
            f"<td style='color:{color};font-weight:700'>{label}</td>"
            "</tr>"
        )
    return rows

# Map signal emoji → CSS-styled HTML for PDF rendering.
# Emoji codepoints are NOT used here — only standard Unicode shapes
# that every font covers, styled with CSS color.
_SIGNAL_HTML = {
    "🟡": '<span style="color:#ca8a04;font-size:15px">●</span>',
    "🟢": '<span style="color:#16a34a;font-size:15px">●</span>',
    "🔵": '<span style="color:#2563eb;font-size:15px">●</span>',
    "🟠": '<span style="color:#ea580c;font-size:15px">●</span>',
    "🔴": '<span style="color:#dc2626;font-size:15px">●</span>',
    "⬆️":  '<span style="color:#6b7280;font-weight:700">↑</span>',
    "⬆️⬆️": '<span style="color:#374151;font-weight:700">↑↑</span>',
    "🚀":  '<span style="color:#7c3aed;font-weight:700">↑↑↑</span>',
    "🚀🚀": '<span style="color:#6d28d9;font-weight:700">↑↑↑↑</span>',
    "👁️":  '<span style="color:#0891b2;font-weight:700">◎</span>',
    "💎":  '<span style="color:#0ea5e9;font-weight:700">◆</span>',
}


# Type-based fallback dots (same visual vocabulary as _SIGNAL_HTML) for when the
# signal field is non-conformant — e.g. authored as text ("Strong BUY") instead of
# an emoji code. Keeps the signal column a clean indicator, never raw words.
_TYPE_FALLBACK_HTML = {
    "BUY":   '<span style="color:#16a34a;font-size:15px">●</span>',
    "SELL":  '<span style="color:#374151;font-weight:700">↑</span>',
    "WATCH": '<span style="color:#0891b2;font-weight:700">◎</span>',
    "HOLD":  '<span style="color:#0ea5e9;font-weight:700">◆</span>',
}

# Longest emoji keys first so "⬆️⬆️"/"🚀🚀" win over their single-char prefixes.
_SIGNAL_KEYS_BY_LEN = sorted(_SIGNAL_HTML, key=len, reverse=True)


def _signal_html(signal: str, atype: str = "") -> str:
    """Render the alert signal as a CSS-drawn colored indicator.

    Signals are *supposed* to be emoji codes (🟢, ⬆️, 👁️…), which map to font-safe
    shapes because raw emoji are invisible on Linux. Authoring drifts though — some
    reports store text ("Strong BUY") or a prefixed form ("BUY 🟡"). Resolution
    order: exact emoji match → any embedded known emoji → a dot derived from the
    row's type. Only if all of those fail does the raw string show.
    """
    signal = (signal or "").strip()
    if signal in _SIGNAL_HTML:
        return _SIGNAL_HTML[signal]
    for key in _SIGNAL_KEYS_BY_LEN:
        if key in signal:                       # e.g. "BUY 🟡" → 🟡
            return _SIGNAL_HTML[key]
    fallback = _TYPE_FALLBACK_HTML.get((atype or "").strip().upper())
    if fallback:
        return fallback
    return signal

# Belief level → background colour
_BELIEF_COLORS = {
    # Strategy A
    "STRONG CONVICTION": "#166534",
    "CONVICTION":        "#1d4ed8",
    "CAUTIOUS":          "#b45309",
    "SKEPTICAL":         "#c2410c",
    "BROKEN":            "#b91c1c",
    # Strategy B
    "MULTIBAGGER":       "#92400e",
    "STRONG COMPOUNDER": "#166534",
    "MARKET BEATER":     "#1d4ed8",
    "DEADWEIGHT":        "#4b5563",
    "TRAP":              "#b91c1c",
    # Strategy C
    "BUY EVERY DIP":     "#166534",
    "LIKE THE STOCK":    "#1d4ed8",
    "MEH":               "#4b5563",
    "GOING DOWNWARD":    "#c2410c",
    "GET OUT":           "#b91c1c",
}

# Alert type → row background
_SIGNAL_BG = {
    "BUY":   "#f0fdf4",
    "SELL":  "#fff1f2",
    "WATCH": "#fefce8",
    "HOLD":  "#f9fafb",
}


def generate_report(data: dict, output_path: Path) -> None:
    """Render data dict → styled PDF at output_path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    html = _render_html(data)
    weasyprint.HTML(string=html, base_url=str(REPORTS_DIR)).write_pdf(str(output_path))
    print(f"Report saved: {output_path}")


def _render_html(d: dict) -> str:
    belief = d.get("belief_level", "")
    belief_b = d.get("belief_b", "")
    belief_c = d.get("belief_c", "")
    primary_belief = belief_b or belief
    belief_color = _BELIEF_COLORS.get(primary_belief, "#4b5563")
    belief_b_color = _BELIEF_COLORS.get(belief_b, belief_color) if belief_b else belief_color
    belief_c_color = _BELIEF_COLORS.get(belief_c, "#4b5563") if belief_c else "#4b5563"
    strategy = d.get("belief_strategy", "?")
    mode = d.get("analysis_mode", "comprehensive")

    # ── Alert levels rows ──────────────────────────────────────────────────
    alert_rows = ""
    for lvl in d.get("alert_levels", []):
        bg = _SIGNAL_BG.get(lvl.get("type", ""), "#f9fafb")
        alert_rows += (
            f'<tr style="background:{bg}">'
            f'<td class="sig">{_signal_html(lvl.get("signal",""), lvl.get("type",""))}</td>'
            f'<td class="price"><b>{lvl.get("price","")}</b></td>'
            f'<td class="atype">{lvl.get("type","")}</td>'
            f'<td>{lvl.get("message","")}</td>'
            f"</tr>"
        )

    # ── Fundamentals rows ──────────────────────────────────────────────────
    fund_rows = ""
    for item in d.get("fundamentals", []):
        fund_rows += (
            f'<tr><td class="label">{item.get("label","")}</td>'
            f'<td><b>{item.get("value","")}</b></td></tr>'
        )

    # ── News items ─────────────────────────────────────────────────────────
    news_html = ""
    for item in d.get("news_catalysts", []):
        url = item.get("url", "")
        headline = item.get("headline", "")
        hl_html = f'<a href="{url}">{headline}</a>' if url else headline
        news_html += (
            f'<div class="news-item">'
            f'<span class="news-date">{item.get("date","")}</span>'
            f'<span class="news-hl">{hl_html}</span>'
            f'<p class="news-sig">{item.get("significance","")}</p>'
            f"</div>"
        )

    # ── Technical key levels ───────────────────────────────────────────────
    tech = d.get("technical_summary", {})
    # Hand-authored levels are now a prose "analyst read" rendered atop the auto
    # Historical S/R card — not two parallel lists in the Technical Summary.
    levels_read = _levels_read_prose(tech)

    # ── Historical S/R (auto-computed: swing-touch zones + Fibonacci) ───────
    try:
        _cp = float(str(d.get("current_price", "")).replace("₹", "").replace(",", "").strip())
    except (TypeError, ValueError):
        _cp = 0.0
    _sr = compute_sr(ANALYSIS_DIR / f'{d.get("ticker", "")}_ohlc_cache.csv', _cp)
    sr_sup_rows = _sr_zone_rows(_sr["support"], "support")
    sr_res_rows = _sr_zone_rows(_sr["resistance"], "resistance")
    sr_fib_rows = _fib_rows(_sr["fib"])
    sr_has = bool(_sr["support"] or _sr["resistance"] or _sr["fib"] or levels_read)

    # ── Floor signal rows ──────────────────────────────────────────────────
    fs        = d.get("floor_signals", {})
    fs_rows   = ""
    _W_COLOR  = {"Higher": "#166534", "Lower": "#c2410c", "Always required": "#1d4ed8"}
    for sig in fs.get("signals", []):
        w     = sig.get("weight", "Normal")
        wc    = _W_COLOR.get(w, "#4b5563")
        fs_rows += (
            f"<tr>"
            f"<td>{sig.get('name','')}</td>"
            f"<td style='text-align:center'><b>{sig.get('stock_pct','')}</b></td>"
            f"<td style='text-align:center;color:#6b7280'>{sig.get('avg_pct','')}</td>"
            f"<td style='color:{wc};font-weight:700'>{w}</td>"
            f"</tr>"
        )

    # ── Sentiment ──────────────────────────────────────────────────────────
    sent = d.get("sentiment", {})

    thesis_changes = d.get("thesis_changes", "")

    # ── Price Forecast rows ──────────────────────────────────────────────
    forecast = d.get("price_forecast", {})
    fc_rows = ""
    for horizon in sorted(forecast.keys(), key=lambda x: int(x)):
        vals = forecast[horizon]
        trend_arrow = {"up": "↑", "down": "↓", "sideways": "→"}.get(vals.get("trend", ""), "→")
        fc_rows += (
            f'<tr>'
            f'<td><b>{horizon} days</b></td>'
            f'<td>₹{vals.get("lower",0):,.0f}</td>'
            f'<td><b>₹{vals.get("predicted",0):,.0f}</b></td>'
            f'<td>₹{vals.get("upper",0):,.0f}</td>'
            f'<td>{trend_arrow} {vals.get("trend","")}</td>'
            f'</tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{d.get("ticker","?")} Analysis — TradeCentral</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    font-size: 13px;
    color: #1f2937;
    background: #f1f5f9;
    line-height: 1.55;
  }}

  /* ── Header ── */
  .header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    color: white;
    padding: 24px 18px 20px;
  }}
  .ticker-name {{ font-size: 30px; font-weight: 800; letter-spacing: 1px; }}
  .company-name {{ font-size: 12px; color: #94a3b8; margin-top: 3px; }}
  .meta-row {{
    display: flex; flex-wrap: wrap; gap: 14px;
    margin-top: 14px;
  }}
  .meta-item {{ font-size: 12px; color: #cbd5e1; }}
  .meta-item b {{ color: #ffffff; }}
  .belief-badge {{
    display: inline-block;
    background: {belief_color};
    color: white;
    padding: 5px 16px;
    border-radius: 99px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.4px;
    margin-top: 16px;
  }}
  .belief-badges {{
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 16px;
  }}
  .belief-badges .belief-badge {{ margin-top: 0; }}
  .gen-info {{ font-size: 10px; color: #64748b; margin-top: 6px; }}
  .mode-badge {{
    display: inline-block;
    background: #374151;
    color: #d1d5db;
    padding: 3px 10px;
    border-radius: 99px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.3px;
    margin-left: 8px;
  }}

  /* ── Section cards ── */
  .card {{
    background: white;
    margin: 10px;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
  }}
  .card-header {{
    background: #1e3a5f;
    color: white;
    padding: 9px 16px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    text-transform: uppercase;
  }}
  .card-body {{ padding: 14px 16px; }}

  /* ── Belief justification ── */
  .belief-text {{
    font-size: 13px;
    line-height: 1.65;
    color: #374151;
    border-left: 4px solid {belief_color};
    padding-left: 12px;
  }}

  /* ── Tables ── */
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{
    background: #f8fafc;
    padding: 7px 10px;
    text-align: left;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: #64748b;
    border-bottom: 1px solid #e2e8f0;
  }}
  td {{
    padding: 8px 10px;
    border-bottom: 1px solid #f1f5f9;
    vertical-align: top;
  }}
  .label {{ color: #6b7280; width: 52%; }}

  /* ── Alert table ── */
  .sig  {{ font-size: 16px; width: 30px; }}
  .price {{ width: 88px; white-space: nowrap; }}
  .atype {{
    width: 46px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }}

  /* ── Technical summary ── */
  .tech-grid {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 12px;
  }}
  .tech-pill {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 12px;
  }}
  .tech-pill span {{ color: #64748b; font-size: 10px; display: block; }}

  /* ── News ── */
  .news-item {{
    padding: 10px 0;
    border-bottom: 1px solid #f1f5f9;
  }}
  .news-item:last-child {{ border-bottom: none; }}
  .news-date {{
    display: block;
    font-size: 10px;
    color: #94a3b8;
    margin-bottom: 2px;
  }}
  .news-hl {{ font-weight: 600; font-size: 13px; }}
  .news-sig {{ font-size: 12px; color: #6b7280; margin-top: 4px; line-height: 1.5; }}

  /* ── Sentiment ── */
  .sent-block {{
    border-radius: 7px;
    padding: 10px 12px;
    margin-bottom: 8px;
  }}
  .sent-block:last-child {{ margin-bottom: 0; }}
  .bull {{ background: #f0fdf4; border-left: 4px solid #22c55e; }}
  .bear {{ background: #fff1f2; border-left: 4px solid #ef4444; }}
  .verdict {{ background: #eff6ff; border-left: 4px solid #3b82f6; }}
  .sent-label {{
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #94a3b8;
    margin-bottom: 4px;
  }}
  .sent-text {{ font-size: 12px; color: #374151; line-height: 1.55; }}

  /* ── Thesis changes ── */
  .thesis-box {{
    background: #fffbeb;
    border-left: 4px solid #f59e0b;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 12px;
    color: #374151;
    line-height: 1.6;
  }}

  /* ── Sable Advisory ── */
  .advisory-card .card-header {{ background: linear-gradient(90deg, #92400e, #d4915c); }}
  .advisory-text {{ font-size: 12px; line-height: 1.8; color: #374151; white-space: pre-wrap; }}

  /* ── Footer ── */
  .footer {{
    text-align: center;
    font-size: 10px;
    color: #94a3b8;
    padding: 16px 10px 20px;
  }}

  .empty {{ color: #94a3b8; font-style: italic; font-size: 12px; }}

  /* ── Floor signals ── */
  .fs-meta {{
    font-size: 11px;
    color: #64748b;
    margin-bottom: 8px;
  }}
  .fs-explain {{
    font-size: 11px;
    color: #6b7280;
    line-height: 1.5;
    margin-bottom: 10px;
    font-style: italic;
  }}

  /* ── Fixed-layout tables (Floor Signals + S/R) — explicit column widths via
       <colgroup> so WeasyPrint wraps at spaces, never mid-word. ── */
  .fs-table {{ table-layout: fixed; width: 100%; }}
  .fs-table td, .fs-table th {{ overflow-wrap: break-word; word-break: normal; }}

  /* ── Historical S/R card ── */
  .sr-sub {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: #64748b;
    font-weight: 700;
    margin: 14px 0 4px;
  }}
  .sr-sub.first {{ margin-top: 0; }}
  .sr-read {{
    font-size: 12.5px;
    line-height: 1.65;
    color: #374151;
    background: #f8fafc;
    border-left: 4px solid #64748b;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 12px;
  }}
  .sr-cap {{
    font-size: 11px;
    color: #6b7280;
    line-height: 1.5;
    margin-bottom: 8px;
    font-style: italic;
  }}
  .fib-confl {{ color: #b45309; font-weight: 700; }}
</style>
</head>
<body>

<!-- ═══ HEADER ═══════════════════════════════════════════════════════════ -->
<div class="header">
  <div class="ticker-name">{d.get("ticker","?")}</div>
  <div class="company-name">{d.get("company_name","")} · NSE: {d.get("ticker","?")}</div>
  <div class="meta-row">
    <div class="meta-item">Price <b>₹{d.get("current_price","—")}</b></div>
    <div class="meta-item">52W <b>₹{d.get("week_52_low","—")} – ₹{d.get("week_52_high","—")}</b></div>
    <div class="meta-item">MCap <b>{d.get("market_cap","—")}</b></div>
    <div class="meta-item">Sector <b>{d.get("sector","—")}</b></div>
  </div>
  {(
    '<div class="belief-badges">'
    '<div class="belief-badge" style="background:' + belief_b_color + '">' + belief_b + '</div>'
    '<div class="belief-badge" style="background:' + belief_c_color + '">' + belief_c + '</div>'
    '</div>'
  ) if (belief_b and belief_c) else (
    '<div class="belief-badge">' + belief + '</div>' if belief else ''
  )}
  <div class="gen-info">
    Strategy {strategy} · TradeCentral · {d.get("generated_date","")}
    <span class="mode-badge">{mode}</span>
  </div>
</div>

<!-- ═══ BELIEF LEVEL ══════════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Belief Level</div><div class="card-body"><div class="belief-text">' + d.get("belief_justification","") + '</div></div></div>') if (belief_b and belief_c) and d.get("belief_justification") else ('<div class="card"><div class="card-header">Belief Level — ' + belief + '</div><div class="card-body"><div class="belief-text">' + d.get("belief_justification","") + '</div></div></div>') if belief and d.get("belief_justification") else ''}

<!-- ═══ TECHNICAL SUMMARY ════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Technical Summary</div><div class="card-body">'
  '<div class="tech-grid">'
  '<div class="tech-pill"><span>Trend</span>' + tech.get("trend","—") + '</div>'
  '<div class="tech-pill"><span>Cycle Position</span>' + tech.get("cycle_position","—") + '</div>'
  '<div class="tech-pill"><span>Pattern</span>' + tech.get("pattern","—") + '</div>'
  '<div class="tech-pill"><span>ATR</span>₹' + tech.get("atr","—") + '</div>'
  '<div class="tech-pill"><span>RSI</span>' + tech.get("rsi","—") + '</div>'
  '</div></div></div>') if tech else ''}

<!-- ═══ HISTORICAL SUPPORT & RESISTANCE (auto-computed) ═══════════════════ -->
{('<div class="card"><div class="card-header">Historical Support &amp; Resistance</div>'
  '<div class="card-body">'
  + (f'<div class="sr-read">{levels_read}</div>' if levels_read else '')
  + '<p class="sr-cap">Computed automatically from this stock\'s daily price history. '
  '<b>Touch-tested zones</b> are prices where it has repeatedly turned — more touches = '
  'stronger. <b>Fibonacci</b> levels are ratio retracements of the 52-week range that markets '
  'tend to respect. A level that is <b>both</b> (★) is the highest-conviction.</p>'
  + (('<div class="sr-sub first">Touch-tested zones</div>'
      '<table class="fs-table">'
      '<colgroup><col style="width:34%"><col style="width:20%">'
      '<col style="width:26%"><col style="width:20%"></colgroup>'
      '<thead><tr><th>Zone</th><th style="text-align:center">Touches</th>'
      '<th style="text-align:center">Last tested</th><th>Type</th></tr></thead>'
      '<tbody>' + sr_sup_rows + sr_res_rows + '</tbody></table>')
     if (sr_sup_rows or sr_res_rows) else '')
  + (('<div class="sr-sub">Fibonacci retracements (52-week range)</div>'
      '<table class="fs-table">'
      '<colgroup><col style="width:58%"><col style="width:22%">'
      '<col style="width:20%"></colgroup>'
      '<thead><tr><th>Level</th><th style="text-align:center">Price</th>'
      '<th>Type</th></tr></thead>'
      '<tbody>' + sr_fib_rows + '</tbody></table>')
     if sr_fib_rows else '')
  + '</div></div>') if sr_has else ''}

<!-- ═══ ALERT LEVELS ══════════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Alert Levels</div>'
  '<div class="card-body" style="padding:0">'
  '<table><thead><tr><th></th><th>Price</th><th>Type</th><th>Message</th></tr></thead>'
  '<tbody>' + alert_rows + '</tbody></table></div></div>') if alert_rows else ''}

<!-- ═══ PRICE FORECAST ══════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Price Forecast (80% CI)</div>'
  '<div class="card-body" style="padding:0">'
  '<table><thead><tr><th>Horizon</th><th>Lower</th><th>Predicted</th><th>Upper</th><th>Trend</th></tr></thead>'
  '<tbody>' + fc_rows + '</tbody></table>'
  '<p style="padding:8px 12px;font-size:0.8em;color:#6b7280">Statistical forecast via Prophet model — '
  'one input alongside technicals, not a price target.</p></div></div>') if fc_rows else ''}

<!-- ═══ FLOOR SIGNAL CALIBRATION ════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Floor Signal Calibration</div><div class="card-body">'
  '<p class="fs-meta">'
  + str(fs.get("floors_found","?")) + ' floors analysed · period: ' + fs.get("period","?")
  + ' · updated: ' + fs.get("last_updated","?") + '</p>'
  '<p class="fs-explain">Signal frequencies at historical price floors for this stock vs portfolio average. '
  '"Weight guidance" shows how heavily each signal should influence support zone confidence ratings.</p>'
  '<table class="fs-table">'
  '<colgroup><col style="width:46%"><col style="width:16%">'
  '<col style="width:18%"><col style="width:20%"></colgroup>'
  '<thead><tr>'
  '<th>Signal</th><th style="text-align:center">This stock</th>'
  '<th style="text-align:center">Portfolio avg</th><th>Weight guidance</th>'
  '</tr></thead><tbody>' + fs_rows + '</tbody></table>'
  '</div></div>') if fs_rows else ''}

<!-- ═══ FUNDAMENTALS ══════════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Fundamentals (Screener.in)</div>'
  '<div class="card-body" style="padding:0">'
  '<table><thead><tr><th>Metric</th><th>Value</th></tr></thead>'
  '<tbody>' + fund_rows + '</tbody></table></div></div>') if fund_rows else ''}

<!-- ═══ NEWS & CATALYSTS ══════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">News &amp; Catalysts</div>'
  '<div class="card-body">' + news_html + '</div></div>') if news_html else ''}

<!-- ═══ COMMUNITY SENTIMENT ══════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Community Sentiment</div><div class="card-body">'
  '<div class="sent-block bull"><div class="sent-label">Bull Case</div>'
  '<div class="sent-text">' + sent.get("bull_case","") + '</div></div>'
  '<div class="sent-block bear"><div class="sent-label">Bear Case</div>'
  '<div class="sent-text">' + sent.get("bear_case","") + '</div></div>'
  '<div class="sent-block verdict"><div class="sent-label">My Read</div>'
  '<div class="sent-text">' + sent.get("verdict","") + '</div></div>'
  '</div></div>') if sent and any(sent.values()) else ''}

<!-- ═══ THESIS CHANGES ════════════════════════════════════════════════════ -->
{('<div class="card"><div class="card-header">Thesis Changes vs Previous Version</div>'
  '<div class="card-body"><div class="thesis-box">' + thesis_changes + '</div></div></div>') if thesis_changes else ''}

<!-- ═══ SABLE ADVISORY ═══════════════════════════════════════════════════ -->
{('<div class="card advisory-card"><div class="card-header">Sable Advisory</div>'
  '<div class="card-body"><div class="advisory-text">' + d.get("sable_advisory","") + '</div></div></div>') if d.get("sable_advisory") else ''}

<div class="footer">
  TradeCentral · {d.get("ticker","?")} · {d.get("generated_date","")} · Personal use only
</div>

</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 report_generator.py <json_data_file> [output.pdf]")
        sys.exit(1)

    data_file = Path(sys.argv[1])
    if not data_file.exists():
        print(f"Error: {data_file} not found")
        sys.exit(1)

    data = json.loads(data_file.read_text(encoding="utf-8"))

    if len(sys.argv) >= 3:
        output_path = Path(sys.argv[2])
    else:
        date_str = datetime.now().strftime("%Y%m%d")
        output_path = REPORTS_DIR / f"{data['ticker']}_{date_str}.pdf"

    generate_report(data, output_path)


if __name__ == "__main__":
    main()
