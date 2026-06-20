/**
 * portfolio.js — Portfolio add/remove/edit operations.
 *
 * Exposes a small Portfolio object the main app uses:
 *   Portfolio.init(onChange)        — wire modal forms; onChange() fires after any mutation
 *   Portfolio.openAdd()             — open the add-ticker modal
 *   Portfolio.openEdit(stock)       — open the edit-identity modal pre-filled
 *   Portfolio.attachItemActions(li, stock)
 *                                   — append edit/remove icons to a stock-item <li>
 */

(() => {
    let onChangeCallback = () => {};

    const addModal       = document.getElementById('add-stock-modal');
    const addForm        = document.getElementById('add-stock-form');
    const addStatusEl    = document.getElementById('add-stock-status');
    const editModal      = document.getElementById('edit-identity-modal');
    const editForm       = document.getElementById('edit-identity-form');
    const editStatusEl   = document.getElementById('edit-identity-status');
    const editTickerEl   = document.getElementById('edit-identity-ticker');

    function init(onChange) {
        onChangeCallback = onChange || (() => {});

        // ── Add modal ──
        document.getElementById('add-stock-btn').addEventListener('click', openAdd);
        addForm.querySelector('.modal-cancel').addEventListener('click', () => addModal.close());
        addForm.addEventListener('submit', handleAddSubmit);

        // ── Edit modal ──
        editForm.querySelector('.modal-cancel').addEventListener('click', () => editModal.close());
        editForm.addEventListener('submit', handleEditSubmit);
    }

    function openAdd() {
        addStatusEl.textContent = '';
        addStatusEl.className = 'modal-status';
        addForm.reset();
        // Re-check the queue_analysis box after reset
        addForm.querySelector('[name="queue_analysis"]').checked = true;
        addModal.showModal();
    }

    function openEdit(stock) {
        editStatusEl.textContent = '';
        editStatusEl.className = 'modal-status';
        editTickerEl.textContent = stock.ticker;
        editForm.querySelector('[name="sector"]').value = stock.sector || '';
        editForm.querySelector('[name="core_pct"]').value = stock.core_pct ?? 0;
        editForm.dataset.ticker = stock.ticker;
        editModal.showModal();
    }

    async function handleAddSubmit(e) {
        e.preventDefault();
        const fd = new FormData(addForm);
        const ticker = (fd.get('ticker') || '').trim().toUpperCase();
        const queueAnalysis = fd.get('queue_analysis') === 'on';

        if (!ticker) return;

        setStatus(addStatusEl, 'Fetching metadata...', 'pending');

        try {
            const resp = await fetch('/api/portfolio/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ticker, queue_analysis: queueAnalysis }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                setStatus(addStatusEl, data.detail || 'Add failed', 'error');
                return;
            }
            const sector = data.metadata?.sector || 'unknown';
            const queued = data.analysis_queued ? '· analysis queued' : '· no analysis';
            setStatus(addStatusEl, `Added ${data.ticker} (${sector}) ${queued}`, 'ok');

            onChangeCallback();
            // Auto-close after a short delay so the user sees the success message
            setTimeout(() => addModal.close(), 1200);
        } catch (err) {
            console.error('Add failed:', err);
            setStatus(addStatusEl, 'Network error', 'error');
        }
    }

    async function handleEditSubmit(e) {
        e.preventDefault();
        const ticker = editForm.dataset.ticker;
        if (!ticker) return;

        const fd = new FormData(editForm);
        const sector = (fd.get('sector') || '').trim();
        const corePct = parseInt(fd.get('core_pct'), 10);

        if (!sector || Number.isNaN(corePct)) return;

        setStatus(editStatusEl, 'Saving...', 'pending');

        try {
            const resp = await fetch(`/api/portfolio/${ticker}/identity`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sector, core_pct: corePct }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                setStatus(editStatusEl, data.detail || 'Save failed', 'error');
                return;
            }
            setStatus(editStatusEl, `Saved ${ticker}`, 'ok');
            onChangeCallback();
            setTimeout(() => editModal.close(), 800);
        } catch (err) {
            console.error('Edit failed:', err);
            setStatus(editStatusEl, 'Network error', 'error');
        }
    }

    async function removeTicker(ticker) {
        const confirmed = window.confirm(
            `Archive ${ticker}?\n\n` +
            `Its watchlist file, KB dossier, analysis sidecars and reports move into ` +
            `archive/${ticker}/ and the bot stops watching it — fully recoverable.`
        );
        if (!confirmed) return;

        try {
            const resp = await fetch(`/api/portfolio/${ticker}`, { method: 'DELETE' });
            const data = await resp.json();
            if (!resp.ok) {
                window.alert(`Remove failed: ${data.detail || resp.statusText}`);
                return;
            }
            onChangeCallback();
        } catch (err) {
            console.error('Remove failed:', err);
            window.alert('Remove failed: network error');
        }
    }

    function attachItemActions(li, stock) {
        const actions = document.createElement('span');
        actions.className = 'stock-item-actions';

        const editBtn = document.createElement('button');
        editBtn.className = 'stock-action-btn stock-action-edit';
        editBtn.title = `Edit ${stock.ticker}`;
        editBtn.textContent = '✎';
        editBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            // Need full identity to pre-fill — fetch single-stock detail
            try {
                const resp = await fetch(`/api/stocks/${stock.ticker}`);
                if (!resp.ok) {
                    window.alert(`Could not load ${stock.ticker} for edit`);
                    return;
                }
                const detail = await resp.json();
                openEdit({
                    ticker: detail.ticker,
                    sector: stock.sector || '',
                    core_pct: detail.core_pct,
                });
            } catch (err) {
                console.error('Edit fetch failed:', err);
                window.alert('Could not load stock for edit');
            }
        });

        const removeBtn = document.createElement('button');
        removeBtn.className = 'stock-action-btn stock-action-remove';
        removeBtn.title = `Remove ${stock.ticker}`;
        removeBtn.textContent = '×';
        removeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            removeTicker(stock.ticker);
        });

        actions.appendChild(editBtn);
        actions.appendChild(removeBtn);
        li.appendChild(actions);
    }

    function setStatus(el, text, kind) {
        el.textContent = text;
        el.className = `modal-status ${kind || ''}`;
    }

    window.Portfolio = {
        init,
        openAdd,
        openEdit,
        attachItemActions,
    };
})();
