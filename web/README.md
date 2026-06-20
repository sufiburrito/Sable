# `web/` — Sable Web UI

A FastAPI + vanilla HTML/CSS/JS dashboard that mirrors the bot's live view in the browser. Designed to be lightweight (no npm / no build step) and to **reuse** the alert bot's logic rather than reimplement it.

## Running it

```bash
python3 web/web.py
```

Serves on **http://localhost:8080**. Run it alongside the bot (`python3 run.py`) — the bot owns market data polling and alert state, the web UI is a read-mostly view layer with portfolio mutations.

## Stack

- **Backend:** FastAPI (single process, no workers)
- **Frontend:** vanilla HTML / CSS / JS — no React, no npm, no build step
- **Charting:** Plotly.js loaded from CDN
- **Fonts:** Outfit (body) + JetBrains Mono (code/numbers), loaded from Google Fonts

## Routes

| Route | Returns |
|-------|---------|
| `/api/stocks` | active stock list + focus-mode background tickers from `data/focus.json` |
| `/api/prices` | per-ticker OHLC for chart rendering (sourced from `alert_bot/ohlc_cache.py`) |
| `/api/alerts` | tail of `data/alerts.jsonl` for the alert log panel |
| `/api/mmi` | current MMI snapshot via `alert_bot/mmi.py` |
| `/api/simulation` | Monte Carlo fan chart percentiles (P5/P25/P50/P75/P95) via `quant_modeling/monte_carlo.py` |
| `/api/regime` | current HMM regime via `quant_modeling/hmm_regime.py` |
| `/api/smartmoney` | insider / promoter / FII-DII signals for the discovery panel (from `data/insider_activity.json` + flow data) |
| `/api/portfolio` | add / remove / edit watchlist entries — delegates to `alert_bot/portfolio.py` |

## Features

- **Interactive candlestick chart** with alert level overlays and in-chart popovers showing the level's message, confidence, and last-fire timestamp
- **Monte Carlo fan chart** overlay for 10-120 day forward projections
- **HMM regime badge** (Bull / Bear / Sideways) drawn from the same `quant_modeling/` modules the alert verdicts use
- **Alert log panel** tailing `data/alerts.jsonl`
- **Timeframe selector** (1D / 1W / 1M / 3M / 6M / 1Y)
- **Discovery / smart-money panel** — surfaces insider, promoter, and FII/DII signals via `/api/smartmoney` (frontend in `web/static/js/discovery.js`)
- **Portfolio editing** — the pencil icon opens an inline editor that calls `/api/portfolio` and auto-syncs the CLAUDE.md Active Stocks table (same backend as the Discord `/portfolio` command)

## Aesthetic — "Void Console"

- Deep blacks throughout
- Amber/copper accents — primary accent color is **`#d4915c`**
- Outfit for headings/body, JetBrains Mono for any monospace surface (prices, tickers, code)

Keep this aesthetic consistent across new UI work; the design language is part of how the app feels.

## Reuse rule (load-bearing)

**API routes import from `alert_bot/` directly — never duplicate logic.**

If you need market data, MMI, regime, alerts, or portfolio operations from the web layer, the source already lives in `alert_bot/*.py` (or `quant_modeling/*.py` for simulation/regime). Import and call it. Reimplementing creates two-source-of-truth bugs — most painfully, drift between the Discord alerts and the web UI's view of the same state.

When something new is needed in both surfaces, build it in `alert_bot/` first and call it from both `main.py` and the web route.
