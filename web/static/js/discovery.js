/**
 * discovery.js — Discovery Matrix bubble chart.
 *
 * Exposes Discovery.show() and Discovery.hide(), called from app.js
 * when the DISCOVERY toggle button is clicked.
 *
 * Chart axes encode quantitative signal validity:
 *   X = capital deployed (₹ Cr, log scale) — commitment proxy
 *   Y = distinct buyers (entity count)     — statistical independence
 *   Bubble size = recency (days_ago)        — signal freshness decay
 *   Color = conviction tier                 — composite 5-factor score
 */

const Discovery = (() => {

    let _data     = null;   // cached /api/discovery response
    let _rendered = false;  // true after first Plotly.newPlot call

    // ── Visual style per conviction tier ──────────────────────────────
    const TIER_STYLES = {
        'HIGH CONVICTION': {
            color: 'rgba(212,145,92,0.20)',
            line:  { color: '#d4915c', width: 2.5 },
        },
        'BUILDING': {
            color: 'rgba(45,212,160,0.15)',
            line:  { color: '#2dd4a0', width: 1.5 },
        },
        'EARLY SIGNAL': {
            color: 'rgba(78,77,72,0.08)',
            line:  { color: '#4e4d48', width: 1.0 },
        },
    };

    const TIER_ORDER = ['HIGH CONVICTION', 'BUILDING', 'EARLY SIGNAL'];

    // Score dimension max values (for dot rendering)
    const SCORE_MAXES = {
        smart_money: 3,
        capital:     2,
        macro:       2,
        technical:   3,
        news:        1,
    };

    // Maps days_ago 0→90 to Plotly marker diameter 44→16px
    // Newer signals are larger bubbles — draws the eye to what matters.
    function _recencySize(daysAgo) {
        const d = Math.min(Math.max(daysAgo, 0), 90);
        return Math.round(44 - (d / 90) * 28);
    }

    // Which quadrant label applies based on capital + entity thresholds
    function _quadrantLabel(valueCr, entityCount) {
        const highCap = valueCr >= 300;
        const highEnt = entityCount >= 5;
        if (highCap && highEnt)  return 'INSTITUTIONAL CONSENSUS';
        if (highCap && !highEnt) return 'WHALE';
        if (!highCap && highEnt) return 'COORDINATED';
        return 'WATCH';
    }

    // Escape HTML special chars before injecting server strings into innerHTML.
    function _esc(s) {
        return String(s ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // Render filled (●) / empty (○) dots for a score dimension.
    // Empty dots carry .empty class so CSS can dim them separately.
    function _scoreDots(score, max) {
        const parts = [];
        for (let i = 0; i < max; i++) {
            parts.push(i < score
                ? '<span>●</span>'
                : '<span class="empty">○</span>');
        }
        return parts.join(' ');
    }

    // ── Plotly chart render ───────────────────────────────────────────
    function _render(candidates) {
        const plotEl = document.getElementById('discovery-plot');
        if (!plotEl) return;

        // Group candidates by tier
        const byTier = {};
        TIER_ORDER.forEach(t => { byTier[t] = []; });
        candidates.forEach(c => {
            const t = c.tier || 'EARLY SIGNAL';
            if (byTier[t]) byTier[t].push(c);
        });

        const traces = TIER_ORDER.map(tier => {
            const group = byTier[tier];
            const style = TIER_STYLES[tier];

            // Build hover text — richer technical detail without a JS callback
            const hoverParts = group.map(c => {
                let line3 = c.stage_desc || '';
                if (c.rsi != null) line3 += (line3 ? ' · ' : '') + `RSI ${c.rsi}`;
                if (c.rs_vs_nifty != null) {
                    const sign = c.rs_vs_nifty >= 0 ? '+' : '';
                    line3 += (line3 ? ' · ' : '') + `${sign}${c.rs_vs_nifty.toFixed(0)}% vs Nifty`;
                }
                return line3;
            });

            return {
                name: tier,
                type: 'scatter',
                mode: 'markers+text',
                x:    group.map(c => c.value_cr),
                y:    group.map(c => c.entity_count),
                text: group.map(c => c.ticker),
                textposition: 'top center',
                textfont: {
                    family: 'JetBrains Mono, monospace',
                    size:   9,
                    color:  '#c8c5b8',
                },
                marker: {
                    size:    group.map(c => _recencySize(c.days_ago)),
                    color:   style.color,
                    line:    style.line,
                    sizemode: 'diameter',
                },
                customdata: group,
                hovertemplate: (
                    '<b style="font-family:JetBrains Mono">%{text}</b><br>' +
                    '₹%{x:,.0f} Cr · %{y} entities<br>' +
                    '%{customdata.stage_desc}<extra></extra>'
                ),
                hoverlabel: {
                    bgcolor:     '#161621',
                    bordercolor: '#d4915c',
                    font: { family: 'JetBrains Mono, monospace', size: 10, color: '#c8c5b8' },
                },
            };
        });

        const layout = {
            paper_bgcolor: '#0f0f18',
            plot_bgcolor:  '#0f0f18',
            margin: { l: 64, r: 28, t: 28, b: 56 },
            font: {
                family: 'JetBrains Mono, monospace',
                size:   9,
                color:  '#5c5a54',
            },
            legend: {
                x: 0.01, y: 0.01,
                bgcolor:     'rgba(0,0,0,0)',
                bordercolor: 'rgba(0,0,0,0)',
                font: { family: 'JetBrains Mono, monospace', size: 9, color: '#5c5a54' },
                orientation: 'h',
            },
            xaxis: {
                title: {
                    text: '₹ Cr (capital deployed)',
                    font: { size: 9, color: '#5c5a54' },
                },
                type:      'log',
                gridcolor: '#161621',
                tickfont:  { size: 9, color: '#4a4840' },
                tickprefix: '₹',
                ticksuffix: 'Cr',
                zeroline:  false,
                showline:  false,
            },
            yaxis: {
                title: {
                    text: 'Buyers (distinct entities)',
                    font: { size: 9, color: '#5c5a54' },
                },
                gridcolor:   '#161621',
                tickfont:    { size: 9, color: '#4a4840' },
                dtick:       1,
                tickformat:  'd',
                zeroline:    false,
                showline:    false,
            },
            shapes: [
                // Vertical divider at ₹300 Cr — threshold for institutional capital
                {
                    type: 'line',
                    x0: 300, x1: 300,
                    y0: 0,   y1: 1, yref: 'paper',
                    line: { color: '#1e1e2a', width: 0.5, dash: 'dot' },
                },
                // Horizontal divider at 5 entities — statistical independence threshold
                {
                    type: 'line',
                    x0: 0, x1: 1, xref: 'paper',
                    y0: 5, y1: 5,
                    line: { color: '#1e1e2a', width: 0.5, dash: 'dot' },
                },
            ],
            annotations: [
                // Quadrant labels — near-invisible (opacity via color: #2e2d28)
                // They orient the reader without competing with bubble data.
                {
                    x: 1, y: 1, xref: 'paper', yref: 'paper',
                    xanchor: 'right', yanchor: 'top',
                    text: 'INSTITUTIONAL CONSENSUS',
                    showarrow: false,
                    font: { family: 'JetBrains Mono, monospace', size: 8, color: '#2e2d28' },
                },
                {
                    x: 1, y: 0, xref: 'paper', yref: 'paper',
                    xanchor: 'right', yanchor: 'bottom',
                    text: 'WHALE',
                    showarrow: false,
                    font: { family: 'JetBrains Mono, monospace', size: 8, color: '#2e2d28' },
                },
                {
                    x: 0, y: 1, xref: 'paper', yref: 'paper',
                    xanchor: 'left', yanchor: 'top',
                    text: 'COORDINATED',
                    showarrow: false,
                    font: { family: 'JetBrains Mono, monospace', size: 8, color: '#2e2d28' },
                },
                {
                    x: 0, y: 0, xref: 'paper', yref: 'paper',
                    xanchor: 'left', yanchor: 'bottom',
                    text: 'WATCH',
                    showarrow: false,
                    font: { family: 'JetBrains Mono, monospace', size: 8, color: '#2e2d28' },
                },
            ],
            hovermode:  'closest',
            showlegend: true,
        };

        Plotly.newPlot('discovery-plot', traces, layout, {
            responsive:     true,
            displayModeBar: false,
        });

        // Click → detail card (attached after newPlot)
        plotEl.on('plotly_click', (event) => {
            if (!event.points || !event.points[0]) return;
            _showDetailCard(event.points[0].customdata);
        });

        _rendered = true;
    }

    // ── Detail card ───────────────────────────────────────────────────
    function _showDetailCard(c) {
        // Remove any existing card first (one at a time)
        const existing = document.getElementById('discovery-detail');
        if (existing) existing.remove();

        const scores    = c.scores || {};
        const quadrant  = _quadrantLabel(c.value_cr, c.entity_count);
        const partyType = c.party_type || 'entity';

        // Score rows: each dimension shown as filled/empty dots
        const scoreRows = Object.entries(SCORE_MAXES).map(([key, max]) => {
            const val   = scores[key] || 0;
            const label = key.replace('_', ' ').toUpperCase();
            return `
                <div class="dd-score-row">
                    <span class="dd-score-key">${label}</span>
                    <span class="dd-score-dots">${_scoreDots(val, max)}</span>
                </div>`;
        }).join('');

        // Technical pill chips
        const techParts = [];
        if (c.stage_desc && c.stage > 0) {
            techParts.push(`<span class="dd-tag">${c.stage_desc}</span>`);
        }
        if (c.rsi != null) {
            techParts.push(`<span class="dd-tag">RSI ${c.rsi}</span>`);
        }
        if (c.rs_vs_nifty != null) {
            const sign = c.rs_vs_nifty >= 0 ? '+' : '';
            techParts.push(`<span class="dd-tag">${sign}${c.rs_vs_nifty.toFixed(0)}% vs Nifty</span>`);
        }
        if (c.current_price) {
            techParts.push(`<span class="dd-tag dd-price">₹${Number(c.current_price).toLocaleString('en-IN')}</span>`);
        }

        const recencyText = c.days_ago < 90
            ? `${c.days_ago}d ago · SEBI T+2 window`
            : 'Signal age &gt;90d · verify before acting';
        const sectorText = c.sector && c.sector !== 'Unknown'
            ? ` · ${c.sector}` : '';

        const card = document.createElement('div');
        card.className = 'discovery-detail';
        card.id        = 'discovery-detail';
        card.innerHTML = `
            <div class="dd-header">
                <div class="dd-header-top">
                    <span class="dd-ticker">${_esc(c.ticker)}</span>
                    <button class="dd-close" title="Close">×</button>
                </div>
                <div class="dd-meta">
                    <span class="dd-conv">${_esc(c.conviction)}/11</span>
                    <span class="dd-sep">·</span>
                    <span class="dd-tier">${_esc(c.tier)}</span>
                </div>
                <div class="dd-tags">
                    <span class="dd-quadrant-tag">${_esc(quadrant)}</span>
                    <span class="dd-party-type ${_esc(partyType)}">${_esc(partyType).toUpperCase()}</span>
                </div>
            </div>
            <div class="dd-body">
                ${c.narrative ? `<div class="dd-narrative">${_esc(c.narrative)}</div>` : ''}
                ${techParts.length ? `<div class="dd-tech">${techParts.join('')}</div>` : ''}
                ${c.macro_reason ? `<div class="dd-macro">Macro: ${_esc(c.macro_reason)}</div>` : ''}
                ${c.news_ref    ? `<div class="dd-macro">News: ${_esc(c.news_ref)}</div>`        : ''}
                <div class="dd-scores">${scoreRows}</div>
                <div class="dd-recency">${recencyText}${_esc(sectorText)}</div>
            </div>
        `;

        // Wire close button
        card.querySelector('.dd-close').addEventListener('click', () => card.remove());

        document.getElementById('discovery-container').appendChild(card);
    }

    // ── Public interface ──────────────────────────────────────────────

    async function show() {
        const container   = document.getElementById('discovery-container');
        const chartHeader = document.getElementById('chart-header');
        const chartCont   = document.getElementById('chart-container');
        const tfGroup     = document.getElementById('timeframe-group');
        const scanDate    = document.getElementById('discovery-scan-date');

        // Swap chart view out, discovery in
        chartHeader.style.display = 'none';
        chartCont.style.display   = 'none';
        tfGroup.style.display     = 'none';
        container.style.display   = 'flex';

        // Fetch on first show
        if (!_data) {
            try {
                const resp = await fetch('/api/discovery');
                _data = await resp.json();
            } catch (e) {
                console.error('Discovery fetch failed:', e);
                return;
            }
        }

        if (scanDate && _data.scan_date) {
            scanDate.textContent = `SCAN: ${_data.scan_date}`;
        }

        const candidates = _data.candidates || [];
        if (!_rendered && candidates.length > 0) {
            _render(candidates);
        } else if (_rendered) {
            const el = document.getElementById('discovery-plot');
            if (el && el.data) Plotly.Plots.resize(el);
        }
    }

    function hide() {
        const container   = document.getElementById('discovery-container');
        const chartHeader = document.getElementById('chart-header');
        const chartCont   = document.getElementById('chart-container');
        const tfGroup     = document.getElementById('timeframe-group');

        container.style.display   = 'none';
        chartHeader.style.display = '';
        chartCont.style.display   = '';
        tfGroup.style.display     = '';

        // Close any open detail card
        const card = document.getElementById('discovery-detail');
        if (card) card.remove();
    }

    return { show, hide };

})();
