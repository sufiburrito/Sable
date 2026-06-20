/**
 * alerts.js — In-chart alert creation popover.
 *
 * When the user clicks on the candlestick chart, a compact popover
 * appears at the click point (clamped within chart bounds) with:
 *   - Type selector (BUY / SELL / WATCH)
 *   - Confidence selector (1-5)
 *   - Note input
 *   - Nearby context: existing alerts and MAs close to the clicked price
 *
 * The chart blurs softly behind the popover for focus.
 */

const Alerts = (() => {
    let _onCreated = null;

    /**
     * One-time setup. Accepts a callback that fires after alert creation
     * (typically reloads the chart).
     */
    function init(onCreatedCallback) {
        _onCreated = onCreatedCallback;
    }

    /**
     * Open the alert popover at the click point.
     *
     * @param {string} ticker    - Stock ticker
     * @param {number} price     - Clicked price level
     * @param {object} clickInfo - { x, y, containerRect } pixel coords
     * @param {object} context   - { levels, priceData } for nearby info
     */
    function open(ticker, price, clickInfo, context) {
        // Close any existing popover first
        close();

        const container = document.getElementById('chart-container');
        if (!container) return;

        // Blur the chart
        container.classList.add('blurred');

        // Build the popover element
        const popover = document.createElement('div');
        popover.className = 'alert-popover';
        popover.id = 'alert-popover';

        // Nearby context: alerts within ±5% and closest MAs
        const nearbyHTML = _buildNearbyContext(price, context);

        popover.innerHTML = `
            <div class="ap-header">
                <span class="ap-ticker">${ticker}</span>
                <span class="ap-price">₹${price}</span>
            </div>
            <div class="ap-row">
                <div class="ap-type-group">
                    <button class="ap-type-btn buy active" data-type="BUY">BUY</button>
                    <button class="ap-type-btn sell" data-type="SELL">SELL</button>
                    <button class="ap-type-btn watch" data-type="WATCH">WATCH</button>
                </div>
                <div class="ap-conf-group">
                    <button class="ap-conf-btn" data-conf="1">1</button>
                    <button class="ap-conf-btn" data-conf="2">2</button>
                    <button class="ap-conf-btn active" data-conf="3">3</button>
                    <button class="ap-conf-btn" data-conf="4">4</button>
                    <button class="ap-conf-btn" data-conf="5">5</button>
                </div>
            </div>
            <input type="text" class="ap-note" id="ap-note" placeholder="Note (optional)...">
            ${nearbyHTML}
            <div class="ap-actions">
                <button class="ap-cancel">Cancel</button>
                <button class="ap-save">Create</button>
            </div>
        `;

        // Append to the chart-area (parent of chart-container) so it
        // floats above the blurred chart without being blurred itself
        const chartArea = container.closest('.chart-area');
        chartArea.appendChild(popover);

        // ── Position: clamp within chart container bounds ──
        requestAnimationFrame(() => {
            const cRect = container.getBoundingClientRect();
            const aRect = chartArea.getBoundingClientRect();
            const pRect = popover.getBoundingClientRect();

            // Click position relative to chart-area
            const clickX = clickInfo.containerRect.left - aRect.left + clickInfo.x;
            const clickY = clickInfo.containerRect.top - aRect.top + clickInfo.y;

            // Clamp so popover stays fully inside the chart container
            const cLeft = cRect.left - aRect.left;
            const cTop  = cRect.top - aRect.top;

            let left = clickX + 12;  // offset slightly right of click
            let top  = clickY - pRect.height / 2;  // centered vertically on click

            // Right edge: don't overflow past chart container right
            if (left + pRect.width > cLeft + cRect.width - 8) {
                left = clickX - pRect.width - 12;  // flip to left of click
            }
            // Left edge
            if (left < cLeft + 8) {
                left = cLeft + 8;
            }
            // Bottom edge
            if (top + pRect.height > cTop + cRect.height - 8) {
                top = cTop + cRect.height - 8 - pRect.height;
            }
            // Top edge
            if (top < cTop + 8) {
                top = cTop + 8;
            }

            popover.style.left = left + 'px';
            popover.style.top = top + 'px';
        });

        // ── Wire up button interactions ──
        let selectedType = 'BUY';
        let selectedConf = 3;

        popover.querySelector('.ap-type-group').addEventListener('click', (e) => {
            const btn = e.target.closest('.ap-type-btn');
            if (!btn) return;
            popover.querySelectorAll('.ap-type-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            selectedType = btn.dataset.type;
        });

        popover.querySelector('.ap-conf-group').addEventListener('click', (e) => {
            const btn = e.target.closest('.ap-conf-btn');
            if (!btn) return;
            popover.querySelectorAll('.ap-conf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            selectedConf = parseInt(btn.dataset.conf);
        });

        // Save
        popover.querySelector('.ap-save').addEventListener('click', async () => {
            const note = popover.querySelector('.ap-note').value.trim();
            const body = { price, alert_type: selectedType, confidence: selectedConf, note };

            try {
                const resp = await fetch(`/api/alerts/${ticker}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) {
                    console.error('Alert creation failed:', await resp.json());
                    return;
                }
                close();
                if (_onCreated) _onCreated();
            } catch (e) {
                console.error('Alert creation error:', e);
            }
        });

        // Cancel
        popover.querySelector('.ap-cancel').addEventListener('click', () => close());

        // Enter in note field → save
        popover.querySelector('.ap-note').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') popover.querySelector('.ap-save').click();
        });

        // Click outside popover → close (listen on chart-area)
        setTimeout(() => {
            chartArea.addEventListener('mousedown', _outsideClickHandler);
        }, 50);

        // Focus the note field
        setTimeout(() => popover.querySelector('.ap-note')?.focus(), 100);
    }

    /**
     * Close the popover and remove blur.
     */
    function close() {
        const popover = document.getElementById('alert-popover');
        if (popover) popover.remove();

        const container = document.getElementById('chart-container');
        if (container) container.classList.remove('blurred');

        const chartArea = document.querySelector('.chart-area');
        if (chartArea) chartArea.removeEventListener('mousedown', _outsideClickHandler);
    }

    /**
     * Handler for clicks outside the popover — closes it.
     */
    function _outsideClickHandler(e) {
        const popover = document.getElementById('alert-popover');
        if (popover && !popover.contains(e.target)) {
            close();
        }
    }

    /**
     * Build the "nearby context" HTML showing existing alerts and MAs
     * close to the clicked price.
     */
    function _buildNearbyContext(price, context) {
        if (!context) return '';

        const items = [];
        const threshold = price * 0.05;  // ±5%

        // Find alerts within range
        if (context.levels) {
            for (const lvl of context.levels) {
                if (Math.abs(lvl.mid - price) <= threshold) {
                    const dist = ((lvl.mid - price) / price * 100).toFixed(1);
                    const sign = dist >= 0 ? '+' : '';
                    items.push(
                        `<span class="ap-ctx-item">` +
                        `<span class="ap-ctx-dot" style="background:${lvl.color}"></span>` +
                        `${lvl.alert_type} ₹${lvl.mid}` +
                        `<span class="ap-ctx-dist">${sign}${dist}%</span>` +
                        `</span>`
                    );
                }
            }
        }

        // Find closest MA values (use the last non-null value)
        if (context.priceData) {
            const mas = [
                { name: 'MA20', values: context.priceData.ma20, color: 'rgba(212,145,92,0.8)' },
                { name: 'MA50', values: context.priceData.ma50, color: 'rgba(140,138,128,0.8)' },
                { name: 'MA200', values: context.priceData.ma200, color: 'rgba(45,212,160,0.7)' },
            ];
            for (const ma of mas) {
                if (!ma.values) continue;
                // Get last non-null value
                let val = null;
                for (let i = ma.values.length - 1; i >= 0; i--) {
                    if (ma.values[i] !== null) { val = ma.values[i]; break; }
                }
                if (val !== null && Math.abs(val - price) <= threshold) {
                    const dist = ((val - price) / price * 100).toFixed(1);
                    const sign = dist >= 0 ? '+' : '';
                    items.push(
                        `<span class="ap-ctx-item">` +
                        `<span class="ap-ctx-dot" style="background:${ma.color}"></span>` +
                        `${ma.name} ₹${val}` +
                        `<span class="ap-ctx-dist">${sign}${dist}%</span>` +
                        `</span>`
                    );
                }
            }
        }

        if (items.length === 0) return '';

        return `<div class="ap-context">
            <span class="ap-ctx-label">Nearby</span>
            ${items.join('')}
        </div>`;
    }

    return { init, open, close };
})();
