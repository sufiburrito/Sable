/**
 * app.js — Main application controller.
 *
 * Wires up the stock sidebar, chart, alert log, MMI bar,
 * and alert creation modal into a single-page app.
 */

(() => {
    // ── State ──
    let currentTicker = null;
    let currentPeriod = '1mo';
    let currentView = 'foreground';   // 'foreground' | 'background' | 'all'
    let allStocks = [];               // full stock list cached from API

    // Timeframe ladder — zoom out upgrades to the next tier
    const PERIOD_LADDER = ['1d', '5d', '1mo', '3mo', '6mo', '1y'];

    // ── DOM refs ──
    const stockList      = document.getElementById('stock-list');
    const chartTicker    = document.getElementById('chart-ticker');
    const chartPrice     = document.getElementById('chart-price');
    const chartBelief    = document.getElementById('chart-belief');
    const chartRegime    = document.getElementById('chart-regime');
    const chartSimStats  = document.getElementById('chart-sim-stats');
    const chartContainer = document.getElementById('chart-container');
    const alertLog       = document.getElementById('alert-log');
    const alertLevelsList= document.getElementById('alert-levels-list');
    const mmiBar         = document.getElementById('mmi-bar');
    const tfGroup        = document.getElementById('timeframe-group');
    const focusTabs      = document.getElementById('focus-tabs');

    // ── Initialize ──
    async function init() {
        // Load stock list
        await loadStocks();

        // Init alert modal (refresh chart after creating an alert)
        Alerts.init(() => {
            if (currentTicker) loadStock(currentTicker);
        });

        // Init portfolio modals (refresh stock list after add/remove/edit)
        Portfolio.init(() => {
            loadStocks();
        });

        // Timeframe button clicks
        tfGroup.addEventListener('click', (e) => {
            const btn = e.target.closest('.tf-btn');
            if (!btn) return;
            tfGroup.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentPeriod = btn.dataset.period;
            if (currentTicker) loadStock(currentTicker);
        });

        // Focus tab clicks (FG / ALL / BG)
        focusTabs.addEventListener('click', (e) => {
            const tab = e.target.closest('.focus-tab');
            if (!tab) return;
            focusTabs.querySelectorAll('.focus-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentView = tab.dataset.view;
            renderStockList();
        });

        // Window resize → resize chart and discovery plot if active
        window.addEventListener('resize', () => {
            Chart.resize('chart-container');
            const discEl = document.getElementById('discovery-plot');
            if (discEl && discEl.data) Plotly.Plots.resize(discEl);
        });

        // DISCOVERY toggle — swaps chart view for the bubble matrix
        const discoveryBtn = document.getElementById('discovery-btn');
        discoveryBtn.addEventListener('click', () => {
            const isActive = discoveryBtn.classList.toggle('active');
            if (isActive) {
                Discovery.show();
            } else {
                Discovery.hide();
                if (currentTicker) loadStock(currentTicker);
            }
        });

        // Load MMI
        loadMMI();
        // Refresh MMI every 5 minutes
        setInterval(loadMMI, 5 * 60 * 1000);
    }

    // ── Load stock list from API ──
    async function loadStocks() {
        try {
            const resp = await fetch('/api/stocks');
            allStocks = await resp.json();
            renderStockList();
        } catch (e) {
            console.error('Failed to load stocks:', e);
        }
    }

    // ── Render filtered stock list into sidebar ──
    function renderStockList() {
        const filtered = allStocks.filter(s => {
            if (currentView === 'all') return true;
            return s.focus === currentView;
        });

        stockList.innerHTML = '';
        for (const s of filtered) {
            const li = document.createElement('li');
            li.className = 'stock-item';
            // In "all" view, dim background stocks so foreground pops
            if (currentView === 'all' && s.focus === 'background') {
                li.classList.add('bg-stock');
            }
            li.dataset.ticker = s.ticker;
            li.innerHTML = `
                <span class="stock-ticker">${s.ticker}</span>
                <span class="stock-meta">
                    <span class="stock-levels-count">${s.level_count} levels</span>
                    ${s.belief ? `<span class="stock-belief">${s.belief}</span>` : ''}
                </span>
            `;
            li.addEventListener('click', () => {
                stockList.querySelectorAll('.stock-item').forEach(el => el.classList.remove('active'));
                li.classList.add('active');
                loadStock(s.ticker);
            });

            // Right-click to toggle between foreground and background
            li.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                toggleFocus(s);
            });

            // Portfolio actions (edit / remove icons, hover-revealed)
            Portfolio.attachItemActions(li, s);

            stockList.appendChild(li);
        }

        // Preserve current selection if it's still visible, otherwise auto-select first
        if (filtered.length > 0) {
            const currentVisible = stockList.querySelector(`[data-ticker="${currentTicker}"]`);
            if (currentVisible) {
                currentVisible.classList.add('active');
            } else {
                stockList.querySelector('.stock-item')?.click();
            }
        }
    }

    // ── Toggle a stock between foreground and background ──
    async function toggleFocus(stock) {
        const newGroup = stock.focus === 'foreground' ? 'background' : 'foreground';
        try {
            await fetch(`/api/focus/${stock.ticker}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ group: newGroup }),
            });
            // Update local cache so re-render reflects the change immediately
            stock.focus = newGroup;
            renderStockList();
        } catch (e) {
            console.error('Failed to toggle focus:', e);
        }
    }

    // ── Load a stock: chart + alerts + alert log ──
    async function loadStock(ticker) {
        currentTicker = ticker;
        _closeLevelPopover();

        // Show loading state
        chartTicker.textContent = ticker;
        chartPrice.textContent = 'Loading...';
        chartBelief.textContent = '';
        chartContainer.innerHTML = '<div class="chart-placeholder">Loading chart...</div>';

        try {
            // Fetch all data in parallel
            // Simulation only makes sense on daily timeframes (3mo, 6mo, 1y)
            const isDailyView = ['3mo', '6mo', '1y'].includes(currentPeriod);
            const fetches = [
                fetch(`/api/prices/${ticker}?period=${currentPeriod}`),
                fetch(`/api/alerts/${ticker}`),
                fetch(`/api/alert-log/${ticker}?limit=50`),
            ];
            if (isDailyView) {
                fetches.push(fetch(`/api/simulate/${ticker}?days=60`));
                fetches.push(fetch(`/api/regime/${ticker}`));
            }

            const responses = await Promise.all(fetches);
            const priceData = await responses[0].json();
            const alertsData = await responses[1].json();
            const logData = await responses[2].json();
            const simData = isDailyView && responses[3]?.ok
                ? await responses[3].json()
                : null;
            const regimeData = isDailyView && responses[4]?.ok
                ? await responses[4].json()
                : null;

            // Update header
            const lastClose = priceData.close[priceData.close.length - 1];
            const prevClose = priceData.close.length > 1
                ? priceData.close[priceData.close.length - 2]
                : lastClose;
            const changePct = ((lastClose - prevClose) / prevClose * 100).toFixed(2);
            const changeColor = changePct >= 0 ? '#22c55e' : '#ef4444';
            const changeSign = changePct >= 0 ? '+' : '';

            chartTicker.textContent = ticker;
            chartPrice.innerHTML = `₹${lastClose.toFixed(2)} <span style="color:${changeColor};font-size:13px">${changeSign}${changePct}%</span>`;

            // Get belief level from stock list item
            const stockItem = stockList.querySelector(`[data-ticker="${ticker}"]`);
            const beliefEl = stockItem?.querySelector('.stock-belief');
            if (beliefEl) {
                chartBelief.textContent = beliefEl.textContent;
                chartBelief.style.display = '';
            } else {
                chartBelief.textContent = '';
                chartBelief.style.display = 'none';
            }

            // Show regime badge if HMM data is available
            if (regimeData) {
                const info = regimeData.regime_info[regimeData.current];
                const prob = (regimeData.probs[regimeData.current] * 100).toFixed(0);
                chartRegime.innerHTML =
                    `<span class="regime-dot" style="background:${info.color}"></span>` +
                    `<span class="regime-name">${regimeData.current}</span>` +
                    `<span class="regime-prob">${prob}%</span>` +
                    `<span class="regime-action">${info.action}</span>`;
                chartRegime.style.display = '';
            } else {
                chartRegime.innerHTML = '';
                chartRegime.style.display = 'none';
            }

            // Show Monte Carlo stats if simulation data is available
            if (simData) {
                const vol = (simData.params.sigma * 100).toFixed(0);
                const p5  = simData.fan[5][simData.fan[5].length - 1];
                const p95 = simData.fan[95][simData.fan[95].length - 1];
                // Show "(regime)" label when simulation used regime-switching dynamics
                const mcLabel = simData.regime_conditional ? '60d MC (regime)' : '60d MC';
                chartSimStats.innerHTML =
                    `<span class="sim-label">${mcLabel}</span> ` +
                    `<span class="sim-range">₹${Math.round(p5)} — ₹${Math.round(p95)}</span> ` +
                    `<span class="sim-vol">${vol}% vol</span>`;
                chartSimStats.style.display = '';
            } else {
                chartSimStats.innerHTML = '';
                chartSimStats.style.display = 'none';
            }

            // Clear container and render chart
            chartContainer.innerHTML = '';
            const levelsArr = alertsData.levels || [];
            Chart.render(
                'chart-container',
                priceData,
                levelsArr,
                logData.alerts || [],
                (price, clickInfo) => Alerts.open(ticker, price, clickInfo, {
                    levels: levelsArr,
                    priceData: priceData,
                }),
                simData,
                // Zoom-out handler: upgrade to the next timeframe tier
                () => {
                    const idx = PERIOD_LADDER.indexOf(currentPeriod);
                    if (idx < PERIOD_LADDER.length - 1) {
                        currentPeriod = PERIOD_LADDER[idx + 1];
                        // Update the active timeframe button to match
                        tfGroup.querySelectorAll('.tf-btn').forEach(b => {
                            b.classList.toggle('active', b.dataset.period === currentPeriod);
                        });
                        loadStock(currentTicker);
                    }
                },
                regimeData,
            );

            // Render alert log
            renderAlertLog(logData.alerts || []);

            // Render alert levels list
            renderAlertLevels(alertsData.levels || []);

        } catch (e) {
            console.error(`Failed to load ${ticker}:`, e);
            chartContainer.innerHTML = `<div class="chart-placeholder">Error loading ${ticker}</div>`;
        }
    }

    // ── Render alert log panel ──
    function renderAlertLog(alerts) {
        if (alerts.length === 0) {
            alertLog.innerHTML = '<div class="alert-empty">No alerts fired yet</div>';
            return;
        }

        alertLog.innerHTML = alerts.map(a => {
            const typeClass = (a.alert_type || '').toLowerCase();
            const priceStr = a.price ? `₹${Number(a.price).toLocaleString('en-IN')}` : '';
            const time = a.time ? a.time.substring(0, 5) : '';
            const date = a.date || '';
            return `
                <div class="alert-entry">
                    <div class="alert-entry-header">
                        <span class="alert-signal">${a.signal || ''}</span>
                        <span class="alert-type ${typeClass}">${a.alert_type || ''}</span>
                        <span class="alert-price">${priceStr}</span>
                        <span class="alert-time">${date} ${time}</span>
                    </div>
                    <div class="alert-message">${a.message || ''}</div>
                </div>
            `;
        }).join('');
    }

    // ── Render alert levels list (sidebar summary) ──
    function renderAlertLevels(levels) {
        if (levels.length === 0) {
            alertLevelsList.innerHTML = '<div class="alert-empty">No alert levels</div>';
            return;
        }

        // Sort by price descending (sells at top, buys at bottom)
        const sorted = [...levels].sort((a, b) => b.mid - a.mid);

        alertLevelsList.innerHTML = sorted.map((l, i) => `
            <div class="level-entry" data-level-idx="${i}">
                <span class="level-dot" style="background:${l.color}"></span>
                <span class="level-price">₹${l.mid}</span>
                <span class="level-type">${l.alert_type}</span>
                <span class="level-source">${l.source === 'manual' ? 'you' : 'claude'}</span>
            </div>
        `).join('');

        // Click handler: show message popover anchored to the clicked entry
        alertLevelsList.querySelectorAll('.level-entry').forEach((el) => {
            el.addEventListener('click', () => {
                const idx = parseInt(el.dataset.levelIdx);
                const level = sorted[idx];
                _showLevelPopover(el, level);
            });
        });
    }

    // ── Level detail popover ──
    // Shows the full alert message to the right of the clicked level entry.
    // Dismissed by clicking the chart, clicking another level, or pressing Esc.

    function _showLevelPopover(anchorEl, level) {
        // Remove any existing popover
        _closeLevelPopover();

        // Mark this entry as selected
        alertLevelsList.querySelectorAll('.level-entry').forEach(el => el.classList.remove('selected'));
        anchorEl.classList.add('selected');

        // Build popover
        const popover = document.createElement('div');
        popover.className = 'level-popover';
        popover.id = 'level-popover';

        const typeClass = (level.alert_type || '').toLowerCase();
        popover.innerHTML = `
            <div class="level-popover-header">
                <span class="level-popover-signal">${level.signal || ''}</span>
                <span class="level-popover-type ${typeClass}">${level.alert_type}</span>
                <span class="level-popover-price">₹${level.mid}</span>
                <span class="level-popover-range">${level.price_str || ''}</span>
            </div>
            <div class="level-popover-body">${level.message || 'No message'}</div>
            <div class="level-popover-meta">
                <span>${level.source === 'manual' ? 'Your alert' : 'Claude analysis'}</span>
                <span>Confidence ${level.confidence}/5</span>
            </div>
        `;

        // Position: to the left of the alert panel, anchored vertically to the entry
        const panelRect = alertLevelsList.closest('.alert-panel').getBoundingClientRect();
        const entryRect = anchorEl.getBoundingClientRect();

        popover.style.position = 'fixed';
        popover.style.right = (window.innerWidth - panelRect.left + 8) + 'px';
        popover.style.top = entryRect.top + 'px';

        document.body.appendChild(popover);

        // Clamp: if popover extends below viewport, shift it up
        requestAnimationFrame(() => {
            const popRect = popover.getBoundingClientRect();
            if (popRect.bottom > window.innerHeight - 12) {
                popover.style.top = (window.innerHeight - 12 - popRect.height) + 'px';
            }
            if (popRect.top < 12) {
                popover.style.top = '12px';
            }
        });
    }

    function _closeLevelPopover() {
        const existing = document.getElementById('level-popover');
        if (existing) existing.remove();
        alertLevelsList.querySelectorAll('.level-entry').forEach(el => el.classList.remove('selected'));
    }

    // Dismiss popover when clicking the chart area
    chartContainer.addEventListener('mousedown', () => _closeLevelPopover());

    // ── Load MMI bar ──
    async function loadMMI() {
        try {
            const resp = await fetch('/api/mmi');
            const data = await resp.json();

            if (!data.available) {
                mmiBar.innerHTML = '<span class="mmi-loading">MMI unavailable</span>';
                return;
            }

            const zoneClass = data.zone.toLowerCase().replace(/\s+/g, '-');
            const dayArrow = data.day_delta >= 0 ? '↑' : '↓';
            const dayClass = data.day_delta >= 0 ? 'up' : 'down';
            const weekArrow = data.week_delta >= 0 ? '↑' : '↓';
            const weekClass = data.week_delta >= 0 ? 'up' : 'down';

            mmiBar.innerHTML = `
                <span class="mmi-value">MMI: ${data.value}</span>
                <span class="mmi-zone ${zoneClass}">${data.zone}</span>
                <span class="mmi-delta">
                    <span class="${dayClass}">${dayArrow} ${Math.abs(data.day_delta)}</span> vs yesterday
                    &nbsp;&middot;&nbsp;
                    <span class="${weekClass}">${weekArrow} ${Math.abs(data.week_delta)}</span> vs last week
                </span>
            `;
        } catch (e) {
            console.error('MMI fetch failed:', e);
            mmiBar.innerHTML = '<span class="mmi-loading">MMI unavailable</span>';
        }
    }

    // ── Keyboard shortcuts ──
    document.addEventListener('keydown', (e) => {
        // Don't capture when typing in an input
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

        // Timeframe shortcuts: 1-6
        const keyMap = { '1': '1d', '2': '5d', '3': '1mo', '4': '3mo', '5': '6mo', '6': '1y' };
        if (keyMap[e.key]) {
            currentPeriod = keyMap[e.key];
            tfGroup.querySelectorAll('.tf-btn').forEach(b => {
                b.classList.toggle('active', b.dataset.period === currentPeriod);
            });
            if (currentTicker) loadStock(currentTicker);
            return;
        }

        // Focus view shortcuts: f = foreground, a = all, b = background
        const viewMap = { f: 'foreground', a: 'all', b: 'background' };
        if (viewMap[e.key]) {
            currentView = viewMap[e.key];
            focusTabs.querySelectorAll('.focus-tab').forEach(t => {
                t.classList.toggle('active', t.dataset.view === currentView);
            });
            renderStockList();
            return;
        }

        // r = refresh
        if (e.key === 'r' && currentTicker) {
            loadStock(currentTicker);
            return;
        }

        // Escape = close modal and/or popover
        if (e.key === 'Escape') {
            Alerts.close();
            _closeLevelPopover();
        }
    });

    // ── Boot ──
    init();
})();
