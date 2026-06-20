# Sable — an autonomous, conviction-first stock-alert & research engine

Sable is a self-hosted equity research and alerting system, designed around an AI analyst persona
("Sable") that runs on [Claude Code](https://claude.com/claude-code). It watches a watchlist of stocks
against hand-authored support/resistance levels, fires actionable BUY/TRIM/WATCH alerts enriched with a
live multi-factor confidence score, and runs an autonomous nightly cycle for per-stock analysis, a
trade journal, tax planning, and out-of-sample track-record measurement.

> **Built for Indian equities (NSE/BSE), but the engine is market-agnostic** — the data adapters and
> knowledge base are pluggable.

## What's in here (and what isn't)

This is the **open-source engine** — the code and framework. It ships **no personal data**: no API
keys, no portfolio, no transactions, no journal entries, no positions. You bring your own.

- **`alert_bot/`** — the live alerting bot: stock-config parser, multi-factor confidence scorer, price
  feed (Zerodha Kite WebSocket with a yfinance fallback), Discord I/O, CalDAV reminders.
- **`journal/`** — a local Obsidian trade journal built from your broker transactions: FIFO realized
  P&L, a missed-call scorecard, execution review, FY-split effective P&L, and Indian-CG tax planning.
- **`forward_*.py`** — an out-of-sample forward-test rig that resolves past calls against real OHLC and
  learns a Bayesian per-class edge (research read, not an alert gate).
- **`datasets/`, `experiments/`** — a persistent dataset store and an ML research sandbox.
- **`docs/`** — methodology: backtest failure modes, FII/DII flows, F&O signals, market breadth, etc.
- **`quant_modeling/`** — HMM regimes, Monte Carlo, factor design notes.

## Setup

1. **Python deps:** `pip install -r requirements.txt` (Python 3.11+).
2. **Secrets:** copy `.env.example` → `.env` and fill in your broker / Discord / data-source keys.
3. **Your context:** copy `CLAUDE.md.example` → `CLAUDE.md` and customize the persona and the *User
   Profile* for your own setup (your market, style, notification channels). `CLAUDE.md` is gitignored —
   it's yours.
4. **Your watchlist:** add stock configs under `stocks/` using `stocks/_TEMPLATE.md` as the format.
5. **Run:** `python3 run.py` for the live bot; `bash install_forward_cron.sh` to schedule the nightly
   research/journal cycle.

Open `CLAUDE.md.example` for the full architecture map and the analytical framework Sable reasons with.

## License

BSD 3-Clause — see `LICENSE`. **Not financial advice.** This is software for your own research; you are
responsible for your decisions.
