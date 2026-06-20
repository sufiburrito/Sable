/**
 * chart.js — Plotly candlestick chart with alert level overlays.
 *
 * Renders OHLCV candlesticks, volume bars, moving averages,
 * horizontal alert level lines, a lightweight CSS crosshair,
 * and a custom zoom/pan toolbar at the bottom-left of the chart.
 */

const Chart = (() => {
    // Current drag mode — persists across re-renders
    let _currentMode = 'pan';

    // Monte Carlo visibility — persists across re-renders
    let _mcVisible = true;

    // Regime shading visibility — persists across re-renders
    let _regimeVisible = true;

    // Fired alert markers visibility — persists across re-renders
    let _alertsVisible = true;

    // Plotly dark theme layout (shared across all renders)
    const BASE_LAYOUT = {
        paper_bgcolor: '#0f0f18',
        plot_bgcolor:  '#0f0f18',
        font: {
            family: 'JetBrains Mono, monospace',
            color:  '#8c8a80',
            size:   10,
        },
        margin: { t: 10, b: 30, l: 70, r: 60 },
        hovermode: 'x unified',
        hoverlabel: {
            bgcolor: '#161621',
            bordercolor: '#2e2e42',
            font: { family: 'JetBrains Mono', size: 11, color: '#e4e0d8' },
        },
        xaxis: {
            gridcolor:  '#161621',
            linecolor:  '#1e1e2e',
            tickcolor:  '#1e1e2e',
            rangeslider: { visible: false },
            type: 'category',          // avoids gaps for non-trading days
            nticks: 12,
        },
        yaxis: {
            gridcolor: '#161621',
            linecolor: '#1e1e2e',
            tickcolor: '#1e1e2e',
            tickprefix: '₹',
            side: 'right',
            title: '',
        },
        // Volume subplot on xaxis2/yaxis2
        xaxis2: {
            gridcolor: '#161621',
            linecolor: '#1e1e2e',
            tickcolor: '#1e1e2e',
            matches: 'x',
            showticklabels: false,
        },
        yaxis2: {
            gridcolor:  'transparent',
            linecolor:  '#1e1e2e',
            tickcolor:  '#1e1e2e',
            showticklabels: false,
            title: '',
        },
        grid: {
            rows: 2, columns: 1,
            subplots: [['xy'], ['x2y2']],
            roworder: 'top to bottom',
            pattern: 'independent',
        },
        // Height ratios: 75% chart, 25% volume
        yaxis_domain:  [0.28, 1.0],
        yaxis2_domain: [0.0, 0.22],
        showlegend: false,
        dragmode: 'pan',
    };

    /**
     * Render the full chart: candles + volume + MAs + alert levels.
     *
     * @param {string}   containerId - DOM element id
     * @param {object}   priceData   - from /api/prices/{ticker}
     * @param {object[]} levels      - from /api/alerts/{ticker}
     * @param {object[]} alertLog    - from /api/alert-log/{ticker}
     * @param {Function} onClickPrice - callback(price) when user clicks chart
     * @param {object|null} simData  - Monte Carlo fan chart from /api/simulate
     * @param {Function|null} onZoomOut   - callback() when user zooms out past data bounds
     * @param {object|null}   regimeData - HMM regime data from /api/regime
     */
    function render(containerId, priceData, levels, alertLog, onClickPrice, simData, onZoomOut, regimeData) {
        const container = document.getElementById(containerId);
        if (!container) return;

        const { dates, open, high, low, close, volume, ma20, ma50, ma200 } = priceData;
        const traces = [];

        // ── Candlestick trace ──
        traces.push({
            type: 'candlestick',
            x: dates,
            open:  open,
            high:  high,
            low:   low,
            close: close,
            increasing: { line: { color: '#2dd4a0', width: 1 }, fillcolor: '#2dd4a0' },
            decreasing: { line: { color: '#f06060', width: 1 }, fillcolor: '#f06060' },
            whiskerwidth: 0.4,
            name: 'OHLC',
            hoverinfo: 'x+text',
            text: dates.map((_, i) =>
                `O:₹${open[i]} H:₹${high[i]} L:₹${low[i]} C:₹${close[i]}`
            ),
            xaxis: 'x',
            yaxis: 'y',
        });

        // ── Volume bars ──
        const volColors = close.map((c, i) =>
            i === 0 ? '#1e1e2e' : (c >= close[i - 1] ? 'rgba(45,212,160,0.25)' : 'rgba(240,96,96,0.25)')
        );
        traces.push({
            type: 'bar',
            x: dates,
            y: volume,
            marker: { color: volColors },
            name: 'Volume',
            hoverinfo: 'skip',
            xaxis: 'x2',
            yaxis: 'y2',
        });

        // ── Moving averages (if available) ──
        if (ma20) {
            traces.push(_maTrace(dates, ma20, 'rgba(212,145,92,0.6)', 'MA20'));
        }
        if (ma50) {
            traces.push(_maTrace(dates, ma50, 'rgba(140,138,128,0.5)', 'MA50'));
        }
        if (ma200) {
            traces.push(_maTrace(dates, ma200, 'rgba(45,212,160,0.4)', 'MA200'));
        }

        // ── Monte Carlo fan chart (forecast cone) ──
        // The fan extends forward from the last candle as filled percentile bands.
        // We generate future x-axis labels (dates) and add three visual layers:
        //   P5–P95 band  (very faint — 90% of simulations fall in here)
        //   P25–P75 band (slightly stronger — 50% of simulations)
        //   P50 line     (the median path)
        // Track trace indices so the toolbar toggle can show/hide them.
        let _mcTraceStart = -1;
        let _mcTraceCount = 0;
        if (simData && simData.fan) {
            _mcTraceStart = traces.length;
            const fanTraces = _buildFanTraces(dates, simData, regimeData);
            traces.push(...fanTraces);
            _mcTraceCount = fanTraces.length;
        }

        // ── Regime background shading + hover trace ──
        // Colored vertical bands behind the candles, tinted by regime.
        // Each band spans from one regime boundary to the next.
        // We also create an invisible scatter trace with one marker per
        // regime span — Plotly's layout shapes don't support hover, so
        // this trace provides regime info tooltips.
        const shapes = [];
        let _regimeShapeCount = 0;
        let _regimeTraceIdx = -1;
        if (regimeData && regimeData.regimes) {
            const { shapes: regimeShapes, hoverTrace } = _buildRegimeShapes(dates, regimeData, close);
            // Apply initial visibility state
            if (!_regimeVisible) {
                regimeShapes.forEach(s => s.opacity = 0);
            }
            shapes.push(...regimeShapes);
            _regimeShapeCount = regimeShapes.length;

            // Add the hover trace (invisible markers with rich tooltips)
            if (hoverTrace) {
                _regimeTraceIdx = traces.length;
                hoverTrace.visible = _regimeVisible;
                traces.push(hoverTrace);
            }
        }

        // ── Alert level lines ──
        const annotations = [];
        for (const lvl of levels) {
            const mid = lvl.mid;
            const color = lvl.color;
            const dashStyle = lvl.source === 'manual' ? 'dash' : 'solid';

            // Horizontal line spanning the full chart
            shapes.push({
                type: 'line',
                x0: 0, x1: 1,
                y0: mid, y1: mid,
                xref: 'paper', yref: 'y',
                line: { color: color, width: 1.5, dash: dashStyle },
                opacity: 0.7,
            });

            // Price label on the left edge (right side reserved for cursor price)
            annotations.push({
                x: 0.0,
                y: mid,
                xref: 'paper', yref: 'y',
                text: `₹${mid}`,
                font: { family: 'JetBrains Mono', size: 10, color: color },
                showarrow: false,
                xanchor: 'right',
                xshift: -4,
                bgcolor: '#0f0f18',
                borderpad: 2,
            });
        }

        // ── Fired alert markers ──
        let _alertTraceIdx = -1;
        const alertDates = [];
        const alertPrices = [];
        const alertTexts = [];
        for (const a of alertLog) {
            // Match alert date/time to a chart x value
            const matchDate = priceData.period === '1d'
                ? `${a.date} ${a.time?.substring(0, 5)}`
                : a.date;
            if (dates.includes(matchDate)) {
                alertDates.push(matchDate);
                alertPrices.push(a.price);
                alertTexts.push(`${a.signal} ${a.alert_type} ₹${a.price}`);
            }
        }
        if (alertDates.length > 0) {
            _alertTraceIdx = traces.length;
            traces.push({
                type: 'scatter',
                mode: 'markers',
                x: alertDates,
                y: alertPrices,
                marker: {
                    symbol: 'diamond',
                    size: 8,
                    color: '#e4e0d8',
                    line: { color: '#d4915c', width: 1.5 },
                },
                text: alertTexts,
                hoverinfo: 'text',
                name: 'Fired',
                visible: _alertsVisible,
                xaxis: 'x',
                yaxis: 'y',
            });
        }

        // ── Assemble layout ──
        const layout = {
            ...BASE_LAYOUT,
            shapes: shapes,
            annotations: annotations,
            // Force y-axis domain (Plotly grid doesn't always respect these)
            yaxis:  { ...BASE_LAYOUT.yaxis,  domain: [0.28, 1.0] },
            yaxis2: { ...BASE_LAYOUT.yaxis2, domain: [0.0, 0.22] },
            xaxis:  { ...BASE_LAYOUT.xaxis },
            xaxis2: { ...BASE_LAYOUT.xaxis2, matches: 'x' },
        };

        const config = {
            responsive: true,
            displayModeBar: false,      // hidden — we use our own toolbar
            scrollZoom: true,
            doubleClick: 'reset',
        };

        Plotly.newPlot(container, traces, layout, config);

        // ── Auto-fit Y axis when user zooms/pans horizontally ──
        // Also detects zoom-out past data bounds to trigger timeframe upgrade.
        let _zoomOutTimer = null;
        container.on('plotly_relayout', (eventData) => {
            _autoFitY(container, priceData, eventData);

            // Detect zoom-out past data bounds: if the left edge of the visible
            // range extends before the first data point (index < 0), the user
            // wants more history than we have loaded. Debounce 300ms so we
            // don't fire during a pinch-zoom gesture.
            if (onZoomOut) {
                const range = eventData['xaxis.range'];
                const range0 = eventData['xaxis.range[0]'];
                const lo = range ? range[0] : range0;

                if (lo !== undefined && lo < -2) {
                    clearTimeout(_zoomOutTimer);
                    _zoomOutTimer = setTimeout(() => onZoomOut(), 300);
                }
            }
        });

        // ── Click handler for alert creation ──
        // Computes price from pixel position using Plotly's y-axis transform
        // (same method as the crosshair price tag). This is more reliable than
        // reading point.y, which can be NaN when clicking on fill polygons
        // like the Monte Carlo toself traces.
        container.on('plotly_click', (data, event) => {
            const nativeEvent = data.event || event;
            if (!nativeEvent) return;

            const rect = container.getBoundingClientRect();
            const mouseY = nativeEvent.clientY - rect.top;

            // Convert pixel Y → price via Plotly's y-axis internals
            const yaxis = container._fullLayout?.yaxis;
            if (!yaxis) return;

            const plotTop = yaxis._offset;
            const plotHeight = yaxis._length;
            const [priceLow, priceHigh] = yaxis._rl;
            const fraction = (mouseY - plotTop) / plotHeight;

            // Only handle clicks within the price subplot (not volume area)
            if (fraction < 0 || fraction > 1) return;

            const price = Math.round((priceHigh - fraction * (priceHigh - priceLow)) * 100) / 100;

            const clickInfo = {
                x: nativeEvent.clientX - rect.left,
                y: nativeEvent.clientY - rect.top,
                containerRect: rect,
            };
            if (onClickPrice) onClickPrice(price, clickInfo);
        });

        // ── CSS crosshair (lightweight, GPU-composited) ──
        _initCrosshair(container);

        // ── Apply MC visibility state (persists across re-renders) ──
        if (_mcTraceStart >= 0 && !_mcVisible) {
            const indices = Array.from({ length: _mcTraceCount }, (_, i) => _mcTraceStart + i);
            Plotly.restyle(container, { visible: false }, indices);
        }

        // ── Build regime legend (only when regime data is present) ──
        if (regimeData && regimeData.regime_info) {
            _buildRegimeLegend(container, regimeData);
        }

        // ── Toolbar in parent .chart-area (outside Plotly's DOM) ──
        _buildToolbar(container, _mcTraceStart, _mcTraceCount, _regimeShapeCount, _regimeTraceIdx, _alertTraceIdx);
    }

    /**
     * Build a moving average trace.
     */
    function _maTrace(dates, maValues, color, name) {
        const filteredDates = [];
        const filteredValues = [];
        for (let i = 0; i < maValues.length; i++) {
            if (maValues[i] !== null) {
                filteredDates.push(dates[i]);
                filteredValues.push(maValues[i]);
            }
        }
        return {
            type: 'scatter',
            mode: 'lines',
            x: filteredDates,
            y: filteredValues,
            line: { color: color, width: 1, dash: 'dot' },
            name: name,
            hoverinfo: 'skip',
            xaxis: 'x',
            yaxis: 'y',
        };
    }

    /**
     * Build Plotly traces for the Monte Carlo fan chart.
     *
     * The fan extends to the right of the last candle as filled percentile
     * bands. When regime data is available, the fan is sliced into segments
     * colored by the expected regime at each future point in time.
     *
     * HOW REGIME COLORING WORKS:
     *
     * The HMM gives us today's regime probabilities (e.g., 87% Bull, 8%
     * Sideways, 4% Bear, 1% Volatile) and a transition matrix (probability
     * of switching between regimes). By multiplying the probability vector
     * by the transition matrix repeatedly, we can project: "What's the
     * expected regime mix 10 days from now? 30 days? 60 days?"
     *
     * At each future day, we blend the 4 regime colors by their probability
     * weights. On day 1 (still 87% Bull), the fan is mostly green. As time
     * passes and probabilities spread out, the color shifts to reflect the
     * expected regime composition — maybe greenish-gray as uncertainty grows.
     *
     * The fan is split into segments (~5 days each) with each segment
     * colored by the blended regime color at its midpoint.
     *
     * @param {string[]} historicalDates - x-axis labels from the price data
     * @param {object}   simData        - response from /api/simulate/{ticker}
     * @param {object|null} regimeData  - HMM regime data (optional)
     */
    function _buildFanTraces(historicalDates, simData, regimeData) {
        const fan = simData.fan;
        const daysForward = simData.days_forward;

        // Generate future x-axis labels.
        const lastDate = historicalDates[historicalDates.length - 1];
        const futureDates = [lastDate];  // day 0 = last historical candle
        for (let i = 1; i <= daysForward; i++) {
            futureDates.push(`+${i}d`);
        }

        const traces = [];

        // ── Compute regime color timeline (if available) ──
        // Projects today's regime probabilities forward through the
        // Markov transition matrix, producing a blended RGBA color
        // for each future day.
        const hasRegime = regimeData && regimeData.probs && regimeData.transitions && regimeData.regime_info;
        let dayColors = null;

        if (hasRegime) {
            dayColors = _projectRegimeColors(
                regimeData.probs,
                regimeData.transitions,
                regimeData.regime_info,
                daysForward,
            );
        }

        // ── Fill bands ──
        // If we have regime colors, split into segments (~5 days each).
        // Each segment is a toself polygon colored by the regime blend
        // at its midpoint. If no regime data, use a single green polygon.
        const SEGMENT_DAYS = 5;

        if (dayColors) {
            // Split into segments for both outer and inner bands
            for (let start = 0; start < futureDates.length - 1; start += SEGMENT_DAYS) {
                const end = Math.min(start + SEGMENT_DAYS, futureDates.length - 1);
                const mid = Math.floor((start + end) / 2);
                const { r, g, b } = dayColors[mid];

                // Slice the dates and fan values for this segment
                const segDates = futureDates.slice(start, end + 1);
                const segDatesRev = [...segDates].reverse();

                // Outer band: P5–P95
                traces.push({
                    type: 'scatter', mode: 'lines',
                    x: [...segDates, ...segDatesRev],
                    y: [
                        ...fan[95].slice(start, end + 1),
                        ...[...fan[5].slice(start, end + 1)].reverse(),
                    ],
                    line: { color: 'transparent', width: 0 },
                    fill: 'toself',
                    fillcolor: `rgba(${r}, ${g}, ${b}, 0.10)`,
                    showlegend: false, hoverinfo: 'skip',
                    xaxis: 'x', yaxis: 'y',
                });

                // Inner band: P25–P75
                traces.push({
                    type: 'scatter', mode: 'lines',
                    x: [...segDates, ...segDatesRev],
                    y: [
                        ...fan[75].slice(start, end + 1),
                        ...[...fan[25].slice(start, end + 1)].reverse(),
                    ],
                    line: { color: 'transparent', width: 0 },
                    fill: 'toself',
                    fillcolor: `rgba(${r}, ${g}, ${b}, 0.18)`,
                    showlegend: false, hoverinfo: 'skip',
                    xaxis: 'x', yaxis: 'y',
                });
            }
        } else {
            // No regime data — single green fill (original behavior)
            const reversedDates = [...futureDates].reverse();

            traces.push({
                type: 'scatter', mode: 'lines',
                x: [...futureDates, ...reversedDates],
                y: [...fan[95], ...[...fan[5]].reverse()],
                line: { color: 'transparent', width: 0 },
                fill: 'toself',
                fillcolor: 'rgba(45, 212, 160, 0.08)',
                showlegend: false, hoverinfo: 'skip',
                xaxis: 'x', yaxis: 'y',
            });

            traces.push({
                type: 'scatter', mode: 'lines',
                x: [...futureDates, ...reversedDates],
                y: [...fan[75], ...[...fan[25]].reverse()],
                line: { color: 'transparent', width: 0 },
                fill: 'toself',
                fillcolor: 'rgba(45, 212, 160, 0.15)',
                showlegend: false, hoverinfo: 'skip',
                xaxis: 'x', yaxis: 'y',
            });
        }

        // ── Hover lines: P95 → P75 → P50 → P25 → P5 ──
        // These are always present regardless of regime coloring.
        // The median line color shifts with regime too.
        const midColor = dayColors
            ? dayColors[Math.floor(daysForward / 2)]
            : { r: 45, g: 212, b: 160 };

        const bandLines = [
            { p: 95, label: 'P95',    opacity: 0.25, width: 0.5, dash: 'solid' },
            { p: 75, label: 'P75',    opacity: 0.35, width: 0.5, dash: 'solid' },
            { p: 50, label: 'Median', opacity: 0.7,  width: 1.5, dash: 'dot'   },
            { p: 25, label: 'P25',    opacity: 0.35, width: 0.5, dash: 'solid' },
            { p: 5,  label: 'P5',     opacity: 0.25, width: 0.5, dash: 'solid' },
        ];
        for (const bl of bandLines) {
            traces.push({
                type: 'scatter', mode: 'lines',
                x: futureDates, y: fan[bl.p],
                line: {
                    color: `rgba(${midColor.r}, ${midColor.g}, ${midColor.b}, ${bl.opacity})`,
                    width: bl.width,
                    dash: bl.dash,
                },
                showlegend: false,
                hovertemplate: `${bl.label}: ₹%{y:.2f}<extra></extra>`,
                xaxis: 'x', yaxis: 'y',
            });
        }

        return traces;
    }

    /**
     * Project regime probabilities forward through the Markov transition
     * matrix and blend regime colors at each future day.
     *
     * WHAT THIS DOES:
     *
     * Today the HMM says: "87% Bull, 8% Sideways, 4% Bear, 1% Volatile."
     * The transition matrix says: "If Bull today, 96% chance Bull tomorrow,
     * 2% Sideways, 1.5% Bear, 0.5% Volatile."
     *
     * By multiplying the probability vector by the matrix repeatedly:
     *   Day 1:  still ~85% Bull → mostly green
     *   Day 10: maybe 70% Bull, 15% Sideways → greenish with gray tint
     *   Day 30: maybe 50% Bull, 25% Sideways, 15% Bear → muted green
     *   Day 60: converges toward steady state → neutral blend
     *
     * At each day, we mix the 4 regime colors (green/red/gray/yellow)
     * weighted by their probability to get a single blended RGB color.
     *
     * @param {object} probs     - today's probabilities {bull: 0.87, ...}
     * @param {object} transmat  - transition matrix {bull: {bull: 0.96, ...}, ...}
     * @param {object} info      - regime metadata with colors {bull: {color: "#2dd4a0"}, ...}
     * @param {number} days      - number of days to project forward
     * @returns {Array<{r, g, b}>} - one blended RGB per day (index 0 = today)
     */
    function _projectRegimeColors(probs, transmat, info, days) {
        // Convert regime names to ordered array for matrix math
        const names = Object.keys(probs).sort();

        // Parse hex colors for each regime → {r, g, b}
        const regimeRGB = {};
        for (const name of names) {
            const hex = info[name]?.color || '#8c8a80';
            regimeRGB[name] = {
                r: parseInt(hex.slice(1, 3), 16),
                g: parseInt(hex.slice(3, 5), 16),
                b: parseInt(hex.slice(5, 7), 16),
            };
        }

        // Build transition matrix as 2D array (names × names)
        // T[i][j] = probability of going FROM regime i TO regime j
        const T = names.map(from =>
            names.map(to => transmat[from]?.[to] || 0)
        );

        // Start with today's probability vector
        let p = names.map(n => probs[n] || 0);

        const colors = [];

        for (let d = 0; d <= days; d++) {
            // Blend colors by probability weights
            let r = 0, g = 0, b = 0;
            for (let i = 0; i < names.length; i++) {
                const rgb = regimeRGB[names[i]];
                r += p[i] * rgb.r;
                g += p[i] * rgb.g;
                b += p[i] * rgb.b;
            }
            colors.push({
                r: Math.round(r),
                g: Math.round(g),
                b: Math.round(b),
            });

            // Advance probability vector one day: p_next = p × T
            // (vector-matrix multiplication)
            if (d < days) {
                const pNext = new Array(names.length).fill(0);
                for (let j = 0; j < names.length; j++) {
                    for (let i = 0; i < names.length; i++) {
                        pNext[j] += p[i] * T[i][j];
                    }
                }
                p = pNext;
            }
        }

        return colors;
    }

    /**
     * Build Plotly shapes + hover trace for regime background coloring.
     *
     * Each regime span becomes a colored rectangle stretching the full
     * height of the price chart. We merge consecutive days of the same
     * regime into a single rectangle for efficiency (a year of daily
     * data would otherwise create 252 individual shapes).
     *
     * Since Plotly layout shapes don't support hover events, we also
     * build an invisible scatter trace with one marker per regime span.
     * Each marker sits at the midpoint of the span and carries a rich
     * hovertemplate showing the regime name, date range, duration,
     * and suggested action (e.g., "Accumulate on dips" for bull).
     *
     * @param {string[]} chartDates  - x-axis labels from price data
     * @param {object}   regimeData  - response from /api/regime/{ticker}
     * @param {number[]} closePrices - closing prices for y-positioning of hover markers
     * @returns {{shapes: object[], hoverTrace: object|null}}
     */
    function _buildRegimeShapes(chartDates, regimeData, closePrices) {
        const shapes = [];
        const regimeDates = regimeData.dates;
        const regimes = regimeData.regimes;
        const info = regimeData.regime_info;

        // Build a lookup: date string → regime name
        const dateToRegime = {};
        for (let i = 0; i < regimeDates.length; i++) {
            if (regimes[i]) {
                dateToRegime[regimeDates[i]] = regimes[i];
            }
        }

        // Walk through the chart's x-axis dates and group consecutive
        // days of the same regime into spans.
        const spans = [];  // {regime, startIdx, endIdx}
        let spanStart = null;
        let spanRegime = null;

        for (let i = 0; i <= chartDates.length; i++) {
            const chartDate = i < chartDates.length ? chartDates[i] : null;
            const dateKey = chartDate ? chartDate.substring(0, 10) : null;
            const regime = dateKey ? (dateToRegime[dateKey] || null) : null;

            if (regime !== spanRegime) {
                if (spanRegime && spanStart !== null && info[spanRegime]) {
                    spans.push({ regime: spanRegime, startIdx: spanStart, endIdx: i });
                }
                spanStart = i;
                spanRegime = regime;
            }
        }

        // Build shapes from spans
        for (const span of spans) {
            const color = info[span.regime].color;
            shapes.push({
                type: 'rect',
                x0: span.startIdx - 0.5,
                x1: span.endIdx - 0.5,
                y0: 0, y1: 1,
                xref: 'x', yref: 'paper',
                fillcolor: color,
                opacity: 0.12,   // visible tint (was 0.04)
                line: { width: 0 },
                layer: 'below',
            });
        }

        // Build invisible hover trace: one marker per regime span
        // positioned at the midpoint x and the average price in that span.
        const hoverX = [];
        const hoverY = [];
        const hoverText = [];
        const hoverColors = [];

        for (const span of spans) {
            const midIdx = Math.floor((span.startIdx + span.endIdx) / 2);
            if (midIdx >= chartDates.length) continue;

            hoverX.push(chartDates[midIdx]);

            // Y position: average closing price in the span (so marker
            // is roughly in the middle of the price action)
            let sumPrice = 0, count = 0;
            for (let i = span.startIdx; i < span.endIdx && i < closePrices.length; i++) {
                sumPrice += closePrices[i];
                count++;
            }
            hoverY.push(count > 0 ? sumPrice / count : closePrices[closePrices.length - 1]);

            const duration = span.endIdx - span.startIdx;
            const startDate = chartDates[span.startIdx]?.substring(0, 10) || '';
            const endDate = chartDates[Math.min(span.endIdx - 1, chartDates.length - 1)]?.substring(0, 10) || '';
            const regimeInfo = info[span.regime];
            const emoji = regimeInfo.emoji || '';
            const action = regimeInfo.action || '';
            const regimeLabel = span.regime.charAt(0).toUpperCase() + span.regime.slice(1);

            hoverText.push(
                `${emoji} <b>${regimeLabel}</b> regime<br>` +
                `${startDate} → ${endDate} (${duration} days)<br>` +
                `<i>${action}</i>`
            );
            hoverColors.push(regimeInfo.color);
        }

        const hoverTrace = hoverX.length > 0 ? {
            type: 'scatter',
            mode: 'markers',
            x: hoverX,
            y: hoverY,
            marker: {
                size: 0.1,          // effectively invisible
                color: 'transparent',
                opacity: 0,
            },
            hoverinfo: 'text',
            hovertemplate: '%{text}<extra></extra>',
            text: hoverText,
            showlegend: false,
            xaxis: 'x', yaxis: 'y',
        } : null;

        return { shapes, hoverTrace };
    }

    // ── Regime Legend ─────────────────────────────────────────────
    // A small overlay in the top-left of the chart area showing
    // what each regime color means. Toggled by the regime toolbar button.

    function _buildRegimeLegend(plotlyContainer, regimeData) {
        const chartArea = plotlyContainer.closest('.chart-area');
        if (!chartArea) return;

        // Remove any previous legend
        chartArea.querySelectorAll('.regime-legend').forEach(el => el.remove());

        const legend = document.createElement('div');
        legend.className = 'regime-legend';
        if (!_regimeVisible) legend.style.display = 'none';

        const info = regimeData.regime_info;
        const probs = regimeData.probs || {};
        const current = regimeData.current;

        // Show all 4 regimes with colored dot + name + probability
        for (const [name, meta] of Object.entries(info)) {
            const row = document.createElement('div');
            row.className = 'regime-legend-row';
            if (name === current) row.classList.add('current');

            const prob = probs[name] !== undefined ? (probs[name] * 100).toFixed(0) : '—';
            row.innerHTML =
                `<span class="regime-legend-dot" style="background:${meta.color}"></span>` +
                `<span class="regime-legend-name">${name.charAt(0).toUpperCase() + name.slice(1)}</span>` +
                `<span class="regime-legend-prob">${prob}%</span>`;

            legend.appendChild(row);
        }

        chartArea.appendChild(legend);
    }

    // ── Crosshair ─────────────────────────────────────────────────
    // Two thin <div> lines positioned via CSS transform — no SVG
    // redraws, runs at display refresh rate via requestAnimationFrame.

    function _initCrosshair(container) {
        const chartArea = container.closest('.chart-area');
        if (!chartArea) return;

        // Clean up any previous crosshair elements
        chartArea.querySelectorAll('.crosshair-h, .crosshair-v, .crosshair-price').forEach(el => el.remove());

        // Create the two lines + price tag
        const hLine = document.createElement('div');
        hLine.className = 'crosshair-h';
        const vLine = document.createElement('div');
        vLine.className = 'crosshair-v';
        const priceTag = document.createElement('div');
        priceTag.className = 'crosshair-price';
        chartArea.appendChild(hLine);
        chartArea.appendChild(vLine);
        chartArea.appendChild(priceTag);

        let rafId = null;

        container.addEventListener('mousemove', (e) => {
            if (rafId) return;  // throttle to one update per frame
            rafId = requestAnimationFrame(() => {
                const rect = container.getBoundingClientRect();
                const areaRect = chartArea.getBoundingClientRect();

                // Mouse position relative to .chart-area
                const x = e.clientX - areaRect.left;
                const y = e.clientY - areaRect.top;

                // Only show if mouse is inside the container
                const inX = e.clientX >= rect.left && e.clientX <= rect.right;
                const inY = e.clientY >= rect.top && e.clientY <= rect.bottom;

                if (inX && inY) {
                    hLine.style.transform = `translateY(${y}px)`;
                    vLine.style.transform = `translateX(${x}px)`;
                    hLine.style.opacity = '1';
                    vLine.style.opacity = '1';

                    // Convert pixel Y → price using Plotly's y-axis layout.
                    // Plotly stores the axis object on the container's _fullLayout.
                    // The yaxis has ._rl (range in data coords) and the plot area
                    // pixel bounds via ._offset and ._length.
                    const yaxis = container._fullLayout?.yaxis;
                    if (yaxis) {
                        // Mouse Y relative to the container (not chart-area)
                        const mouseY = e.clientY - rect.top;
                        // yaxis._offset = top of plot area (px from container top)
                        // yaxis._length = height of plot area in px
                        // yaxis._rl = [bottom_price, top_price] (range, low to high)
                        const plotTop = yaxis._offset;
                        const plotHeight = yaxis._length;
                        const [priceLow, priceHigh] = yaxis._rl;

                        // Linear interpolation: top of plot = priceHigh, bottom = priceLow
                        // (pixel Y increases downward, price increases upward)
                        const fraction = (mouseY - plotTop) / plotHeight;
                        const price = priceHigh - fraction * (priceHigh - priceLow);

                        // Only show the tag if cursor is within the price subplot
                        // (not in the volume subplot below)
                        if (fraction >= 0 && fraction <= 1) {
                            priceTag.textContent = `₹${price.toFixed(2)}`;
                            priceTag.style.transform = `translateY(${y}px)`;
                            priceTag.style.opacity = '1';
                        } else {
                            priceTag.style.opacity = '0';
                        }
                    }
                } else {
                    hLine.style.opacity = '0';
                    vLine.style.opacity = '0';
                    priceTag.style.opacity = '0';
                }
                rafId = null;
            });
        });

        // Hide crosshair when mouse leaves the chart entirely
        container.addEventListener('mouseleave', () => {
            hLine.style.opacity = '0';
            vLine.style.opacity = '0';
            priceTag.style.opacity = '0';
        });
    }

    // ── Auto-fit Y ────────────────────────────────────────────────
    // When the user ZOOMS horizontally, auto-scale the price axis
    // to frame the visible candles (TradingView-style). Skips plain
    // pans — detected by checking if the x-range width changed.

    let _prevRangeWidth = null;

    function _autoFitY(container, priceData, eventData) {
        // Reset on full autorange (double-click reset, toolbar reset)
        if (eventData['xaxis.autorange']) {
            _prevRangeWidth = null;
            return;
        }

        const range = eventData['xaxis.range'];
        const range0 = eventData['xaxis.range[0]'];
        const range1 = eventData['xaxis.range[1]'];

        let lo, hi;
        if (range) {
            lo = range[0]; hi = range[1];
        } else if (range0 !== undefined && range1 !== undefined) {
            lo = range0; hi = range1;
        } else {
            return;  // not a horizontal range event
        }

        // Detect zoom vs pan: zoom changes the range width, pan doesn't
        const width = hi - lo;
        if (_prevRangeWidth !== null && Math.abs(width - _prevRangeWidth) < 0.01) {
            _prevRangeWidth = width;
            return;  // pure pan — skip auto-fit
        }
        _prevRangeWidth = width;

        // Category axis: range values are numeric indices (possibly fractional)
        const iStart = Math.max(0, Math.floor(lo));
        const iEnd = Math.min(priceData.dates.length - 1, Math.ceil(hi));
        if (iStart >= iEnd) return;

        // Find min low / max high in the visible range
        let minLow = Infinity, maxHigh = -Infinity;
        for (let i = iStart; i <= iEnd; i++) {
            if (priceData.low[i] < minLow)   minLow  = priceData.low[i];
            if (priceData.high[i] > maxHigh)  maxHigh = priceData.high[i];
        }

        // 3% padding so candles don't touch axis edges
        const pad = (maxHigh - minLow) * 0.03;
        Plotly.relayout(container, {
            'yaxis.range': [minLow - pad, maxHigh + pad],
            'yaxis.autorange': false,
        });
    }

    // ── Toolbar ───────────────────────────────────────────────────
    // Built as a child of .chart-area (sibling of #chart-container)
    // so it floats above Plotly's SVG layers without z-index fights.

    function _buildToolbar(plotlyContainer, mcTraceStart, mcTraceCount, regimeShapeCount, regimeTraceIdx, alertTraceIdx) {
        const chartArea = plotlyContainer.closest('.chart-area');
        if (!chartArea) return;

        // Remove any previous toolbar
        const existing = chartArea.querySelector('.chart-toolbar');
        if (existing) existing.remove();

        const toolbar = document.createElement('div');
        toolbar.className = 'chart-toolbar';

        const buttons = [
            {
                id: 'pan', label: 'Pan',
                mode: 'pan',
                svg: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="15 19 12 22 9 19"/><polyline points="19 9 22 12 19 15"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/></svg>`,
            },
            {
                id: 'zoom', label: 'Box Zoom',
                mode: 'zoom',
                svg: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>`,
            },
            {
                id: 'hzoom', label: 'H-Zoom',
                mode: 'zoom',
                axis: 'x',
                svg: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 2 12 6 15"/><polyline points="18 9 22 12 18 15"/><line x1="2" y1="12" x2="22" y2="12"/></svg>`,
            },
            {
                id: 'vzoom', label: 'V-Zoom',
                mode: 'zoom',
                axis: 'y',
                svg: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 12 2 15 6"/><polyline points="9 18 12 22 15 18"/><line x1="12" y1="2" x2="12" y2="22"/></svg>`,
            },
            {
                id: 'reset', label: 'Reset',
                action: 'reset',
                svg: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>`,
            },
        ];

        buttons.forEach((btn) => {
            const el = document.createElement('button');
            el.className = 'toolbar-btn' + (btn.id === _currentMode ? ' active' : '');
            el.dataset.tool = btn.id;
            el.title = btn.label;
            el.innerHTML = btn.svg;

            el.addEventListener('click', () => {
                if (btn.action === 'reset') {
                    Plotly.relayout(plotlyContainer, {
                        'xaxis.autorange': true,
                        'yaxis.autorange': true,
                    });
                    return;
                }

                _currentMode = btn.id;

                // Update active highlight on all buttons
                toolbar.querySelectorAll('.toolbar-btn').forEach(b => {
                    b.classList.toggle('active', b.dataset.tool === _currentMode);
                });

                // Apply Plotly dragmode + axis constraints
                const update = { dragmode: btn.mode };
                if (btn.axis) {
                    update['xaxis.fixedrange'] = btn.axis !== 'x';
                    update['yaxis.fixedrange'] = btn.axis !== 'y';
                } else {
                    update['xaxis.fixedrange'] = false;
                    update['yaxis.fixedrange'] = false;
                }

                Plotly.relayout(plotlyContainer, update);
            });

            toolbar.appendChild(el);
        });

        // ── Monte Carlo toggle (only shown when MC data is available) ──
        if (mcTraceStart >= 0 && mcTraceCount > 0) {
            // Visual separator between nav tools and overlays
            const sep = document.createElement('div');
            sep.className = 'toolbar-sep';
            toolbar.appendChild(sep);

            const mcBtn = document.createElement('button');
            mcBtn.className = 'toolbar-btn toolbar-toggle' + (_mcVisible ? ' active' : '');
            mcBtn.dataset.tool = 'mc';
            mcBtn.title = 'Monte Carlo forecast';
            // Cone/fan icon — a simple forward-pointing wedge
            mcBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h2l4-7 4 14 4-10 4 3h3"/></svg>`;

            mcBtn.addEventListener('click', () => {
                _mcVisible = !_mcVisible;
                mcBtn.classList.toggle('active', _mcVisible);

                // Toggle visibility of all MC traces at once
                const indices = Array.from({ length: mcTraceCount }, (_, i) => mcTraceStart + i);
                Plotly.restyle(plotlyContainer, { visible: _mcVisible }, indices);
            });

            toolbar.appendChild(mcBtn);
        }

        // ── Regime shading toggle ──
        if (regimeShapeCount > 0) {
            // Add separator if MC toggle wasn't already added (which adds its own)
            if (!(mcTraceStart >= 0 && mcTraceCount > 0)) {
                const sep = document.createElement('div');
                sep.className = 'toolbar-sep';
                toolbar.appendChild(sep);
            }

            const regBtn = document.createElement('button');
            regBtn.className = 'toolbar-btn toolbar-toggle' + (_regimeVisible ? ' active' : '');
            regBtn.dataset.tool = 'regime';
            regBtn.title = 'HMM regime shading';
            // Layers icon — represents regime bands
            regBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="5" rx="1"/><rect x="3" y="10" width="18" height="5" rx="1"/><rect x="3" y="17" width="18" height="5" rx="1"/></svg>`;

            regBtn.addEventListener('click', () => {
                _regimeVisible = !_regimeVisible;
                regBtn.classList.toggle('active', _regimeVisible);

                // Toggle regime shapes by updating their opacity.
                // Shapes live in the layout, not as traces, so we use relayout.
                const shapeUpdate = {};
                for (let i = 0; i < regimeShapeCount; i++) {
                    shapeUpdate[`shapes[${i}].opacity`] = _regimeVisible ? 0.12 : 0;
                }
                Plotly.relayout(plotlyContainer, shapeUpdate);

                // Toggle the regime hover trace visibility
                if (regimeTraceIdx >= 0) {
                    Plotly.restyle(plotlyContainer, { visible: _regimeVisible }, [regimeTraceIdx]);
                }

                // Toggle the regime legend
                const chartArea = plotlyContainer.closest('.chart-area');
                const legend = chartArea?.querySelector('.regime-legend');
                if (legend) {
                    legend.style.display = _regimeVisible ? '' : 'none';
                }
            });

            toolbar.appendChild(regBtn);
        }

        // ── Fired alerts toggle (only shown when alert markers exist) ──
        if (alertTraceIdx >= 0) {
            // Add separator if no other toggle added one yet
            if (!(mcTraceStart >= 0 && mcTraceCount > 0) && !(regimeShapeCount > 0)) {
                const sep = document.createElement('div');
                sep.className = 'toolbar-sep';
                toolbar.appendChild(sep);
            }

            const alertBtn = document.createElement('button');
            alertBtn.className = 'toolbar-btn toolbar-toggle' + (_alertsVisible ? ' active' : '');
            alertBtn.dataset.tool = 'alerts';
            alertBtn.title = 'Fired alert markers';
            // Diamond icon — matches the marker shape on the chart
            alertBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l10 10-10 10L2 12z"/></svg>`;

            alertBtn.addEventListener('click', () => {
                _alertsVisible = !_alertsVisible;
                alertBtn.classList.toggle('active', _alertsVisible);
                Plotly.restyle(plotlyContainer, { visible: _alertsVisible }, [alertTraceIdx]);
            });

            toolbar.appendChild(alertBtn);
        }

        chartArea.appendChild(toolbar);
    }

    /**
     * Resize the chart to fill its container.
     */
    function resize(containerId) {
        const container = document.getElementById(containerId);
        if (container && container.data) {
            Plotly.Plots.resize(container);
        }
    }

    return { render, resize };
})();
