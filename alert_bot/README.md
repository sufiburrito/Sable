# `alert_bot/` — bot internals reference

This README is the operating reference for the Python bot — the *how* behind the capabilities listed in the Capability Manifest of the project's root `CLAUDE.md`. It collects the mechanical detail (module boundaries, file paths, line numbers, polling rules, format specs) that is rarely needed at compose-time but load-bearing when actually editing the bot.

If you only need to know *what* the bot can do, read the Capability Manifest in `CLAUDE.md`. If you need to change *how* it does it, read here.

---

## Architecture

- **Entry point:** `run.py` → `alert_bot/main.py`
- **Config source:** Stock `.md` files in `stocks/` subdirectory (gitignored — each instance has its own)
- **Markdown IS the config** — parser reads alert levels directly from each stock's `.md` file
- **State persistence:** `data/state.json` (cooldowns survive restarts)
- **Alert log:** `data/alerts.jsonl` (append-only, read by TUI)
- **Credentials:** `.env` file (copy from `.env.example`)

## Module layout

```
alert_bot/
  main.py           — scheduler loop, hot-reload watcher, nightly refresh trigger
  config.py         — env vars, paths, market hours, approach + nightly refresh constants
  parser.py         — markdown → StockConfig / GoldConfig dataclasses
  engine.py         — crossing detection, cooldowns, calendar alerts, approach alerts
  state.py          — load/save state.json
  discord_client.py — single Discord gateway: ingest + commands + reactions + alert posting (see docs/discord_io.md)
  discord_notifier.py — in-process Discord sender (bridges to the client loop) + html_to_markdown + length-split
  discord_webhook.py — out-of-process sender (#sable-broadcast webhook) for send_message.py / send_report.py / subprocess callers
  notifier.py       — legacy Telegram sender — RETIRED (chat surface migrated to Discord); kept unwired
  listener.py       — command handlers (/alert /analyze /react /custom /mode /discover /portfolio); dispatched by discord_client
  portfolio.py      — shared add/remove/archive/restore backend for /portfolio (Discord) and /api/portfolio (Web UI); archive sweeps a ticker's full footprint into archive/{TICKER}/ + registers it in archive/_index.json (suppression source of truth); auto-syncs the README.md Active stocks table; CLI: python3 -m alert_bot.portfolio add|archive|restore|remove|archived|sync [TICKER]
  portfolio_context.py — position-aware alert enrichment; reads portfolio.db AND stocks/{TICKER}.md (Core %/swing split) at fire-time; returns qty/avg-cost/P&L%/core-swing summary or None if not held; called in main.py after floor_hint; failure always silent (never blocks core alert)
  custom_alerts.py  — user-defined alerts via /alert command, persisted to data/custom_alerts.json
  feedback.py       — emoji reaction logging → data/feedback.jsonl + sent_alerts.json registry; ConversationStore for replies
  floor_context.py  — ATR floor/ceiling hints; blends backtest median_dd into BUY alerts
  ohlc_cache.py     — shared OHLC CSV cache with incremental yfinance fetching (analysis/TICKER_ohlc_cache.csv)
  forecaster.py     — ExponentialSmoothing (trend) + Prophet (long-term) wrapper; outputs TrendForecast
  caldav_sync.py    — Radicale CalDAV daemon (in-process); writes .ics files; VEVENT/VTODO with UID-keyed graduation
  regime_context.py — bridges quant_modeling/ HMM + Monte Carlo to alert verdicts ("HIGH CONVICTION", "FALLING KNIFE")
  confidence.py     — multi-factor live confidence at fire-time; core factors (trend, momentum, volume, regime, level, relative strength, MMI, insider) plus Phase-2 calibrated factors below
  calibrate.py      — IC-weights the confidence factors from a calibration set (a factor's weight tracks its measured information coefficient, not a hand-picked constant)
  flow_regime.py    — FII/DII 6-regime classifier (Net Buyer / Net Seller / DII Absorption / Dual Buying / Dual Selling / Transition) — confidence Factor
  breadth_score.py  — 5-component market-breadth health score (A/D, % above 200-DMA, new highs/lows, sector participation, divergence) — confidence Factor
  fundamental_score.py — 1-10 fundamental quality score from market.db — confidence Factor
  vcp_scorer.py     — Minervini VCP (volatility contraction) score — confidence Factor + /analyze input
  trade_levels.py   — derives the optional TRADE: target/stop overlay for clean swings (R:R-gated; ATR/structural stop, MFE-shrunk target capped at the regime Monte-Carlo p75 cone)
  mmi.py            — TickerTape Market Mood Index scraper
  digest.py         — Dalal Street morning digest sector lookup (digest itself is processed by autonomous loop)
  gold.py           — Gold tracker (5-factor scorecard, 3-state regime, ₹/gram view)

  # Price feed (real-time Kite WebSocket, yfinance fallback) — see docs/readme.ZERODHA_API.md
  price_feed.py     — PriceFeed Protocol + create_price_feed() factory (selects Kite → Groww → yfinance)
  kite_auth.py      — Zerodha Kite Connect TOTP auto-login + token persistence (kiteconnect + pyotp)
  kite_feed.py      — KitePriceFeed: WebSocket tick cache with REST fallback, dynamic poll interval
  groww_auth.py     — Groww TOTP login + token persistence
  groww_feed.py     — GrowwPriceFeed: Groww SDK price feed (secondary real-time source)
```

## Approach alerts (auto-calibrated)

For quiet/low-volatility stocks, "approaching" alerts auto-tune so they don't spam. Config constants in `alert_bot/config.py:50-56`:

- `APPROACH_DEAD_ZONE_PCT` — minimum % distance from level before approach can fire
- `APPROACH_MAX_RECENT_ALERTS` — cap on alerts fired in lookback window
- `APPROACH_ATR_MULTIPLIER` — proximity threshold scales with ATR (active stocks fire wider, quiet stocks tighter)
- `APPROACH_COOLDOWN_HOURS` — minimum gap between approach alerts on the same level

## Nightly refresh pipeline

- **Trigger:** 11 PM IST on trading days (`config.py:NIGHTLY_REFRESH_HOUR=23`)
- **Stocks:** `config/active_refresh_stocks.txt` (curated subset, not all of `stocks/*.md`)
- **Mode:** `chart-news-community-retro-backtest-forecast` (heavyweight)
- **Mechanism:** `alert_bot/main.py` writes `requests/{TICKER}_nightly.json` per stock
- **Consumer:** autonomous loop (`LOOP_PROMPT.md` Step B) processes via `Claude_MAINFLOW.md`
- **Output:** updated `stocks/{TICKER}.md` (in-place merge), `reports/{TICKER}_data.json`, `reports/{TICKER}_YYYYMMDD.pdf` posted to `#sable-broadcast`

## Notification format (Discord — see docs/discord_io.md)

- Price alerts:    `{signal_emoji}  {BUY|SELL|WATCH}  {message}`
- Calendar alerts: `📅  REMINDER  {message}`
- MMI alerts:      `{zone_emoji} MMI: {value} — {zone}  ↑/↓ vs yesterday/last week`
- Startup:         table of current prices + MMI snapshot
- HTML in messages is translated to Discord Markdown centrally at the send boundary; messages over 2000 chars auto-split.

## Reply chat (`feedback.py:ConversationStore`)

- Reply directly to any alert message in Discord → message + alert context logged to `data/conversations.jsonl`
- Sable answers in the channel the reply was made in (channel-local reply rule)
- Used by autonomous loop chat mode (`chat: true` in request JSON) for free-form Q&A on alerts and analysis

## MMI integration

- Source: TickerTape — scraped from `__NEXT_DATA__` JSON embedded in page HTML (no API key)
- Alerts on zone change or ≥5pt move with 30-min cooldown
- Displayed in TUI MMIBar (second line)
- Zones: <30 Extreme Fear 🟢, 30-50 Fear 🟡, 50-70 Greed 🟠, ≥70 Extreme Greed 🔴

## Discord ingest (`discord_ingest.py`)

Watches 3 Discord channels with 5-minute debounce, saves forwarded messages as date-named files:

- `#dalal-digest`   → `dalalstreet_morning/YYYY-MM-DD.md`
- `#insider-info`   → `insider_trades/YYYY-MM-DD.md`
- `#general-intel`  → `intel_inbox/YYYY-MM-DD_HHMMSS.md`

Run as a separate process (not part of `run.py`). Replaces manual file dropping for daily inputs. Full mechanics live in the `discord_ingest.py` module docstring.

## Dalal Street morning digest

- **Directory:** `dalalstreet_morning/` — drop a `.md` file named by date (e.g. `2026-04-07.md`)
- **Processing:** Claude in the autonomous loop (`LOOP_PROMPT.md` Step A) — NOT the Python bot
- **Sector lookup:** `data/stock_sectors.json` — built at bot startup by `alert_bot/digest.py:build_sector_lookup()`
- **Never re-processes** — processed dates stored in `state.digest_processed` (persisted in `data/state.json`)

### How it works

1. The autonomous loop checks `dalalstreet_morning/` for new date-named `.md` files each cycle
2. Claude reads the commentary + `data/stock_sectors.json` + the Active stocks table in README.md
3. Claude **reasons about second-order connections** — not keyword matching, but causal chains:
   - Crude oil rising → input cost pressure on petrochemical-feedstock and metals names
   - RBI rate policy → capex-sensitive names (power capex, rail/telecom infra, real estate)
   - FII outflows → which holdings carry high FII ownership and are most exposed
   - Geopolitical tension → defence names and strategic supply chains
   - Currency moves → export earners vs import-dependent names
   - Sector themes → renewables, telecom/5G, defence
4. Sends a Discord digest with MACRO data points + PORTFOLIO IMPACT (↑/↓ per stock with causal reasoning) + ACTIONABLE items if warranted
5. Marks the date as processed in `data/state.json`

### Digest format

```
📰  DALAL STREET DIGEST
Tuesday, April 7, 2026

📊  MACRO           (FII/DII flows, crude, gold, global markets — with numbers)
📌  PORTFOLIO IMPACT (5+ stocks with ↑/↓ and one-line causal connection)
⚡  ACTIONABLE       (only if a stock is near an alert level AND macro supports/contradicts)
🔍  EXPLORE          (2-3 non-portfolio stocks from data/stock_universe.json that benefit from today's themes)
```

### Stock universe (`data/stock_universe.json`)

- Curated lookup of ~100 NSE stocks grouped by thematic buckets (not just sectors)
- **Tier 1**: Index constituents, liquid, safe suggestions
- **Tier 2**: Small/mid-caps with multi-bagger traits — niche monopolies, emerging sectors
- Excludes all portfolio stocks (listed in `_meta.portfolio_excluded`)
- Claude can go off-menu with `(unlisted — verify before acting)` flag
- Refresh periodically in a dedicated session when gaps are noticed

## Key design decisions

- BUY fires when price drops FROM ABOVE into/below level (uses `upper` bound for ranges)
- SELL fires when price rises FROM BELOW into/above level (uses `lower` bound for ranges)
- WATCH fires either direction into the band
- 30-min cooldown per level (persisted in `state.json`)
- Warming-up: first poll sets `prev_prices`, no alerts fire until second poll
- **Hot-reload:** `parser.py` re-reads `stocks/*.md` whenever the directory mtime changes (`alert_bot/main.py:346` `_stocks_dir_mtime()`); takes effect within one poll cycle (3-5 min). Plus a full reload at market open daily.
- **Cooldown keys are whitespace-sensitive** (`alert_bot/state.py:67-68`): the per-level cooldown key is `f"{ticker}:{price_str}"` using the *raw markdown text*. Editing `₹165-200` → `₹165 - 200` in a stock `.md` silently resets the cooldown and the level re-fires immediately. Match the existing price-cell formatting exactly when editing.
- **Disarm expiry only runs while the bot is polling** (`alert_bot/state.py:97-128`, `DISARM_EXPIRY_HOURS=4`): auto-rearm happens during `is_level_disarmed()` checks in-poll. If the bot is offline >4h, the disarm survives until the next poll re-checks — there is no startup re-evaluation. Plan around this for long downtimes.
- **Parser silently skips malformed rows** (`alert_bot/parser.py:131`): bad column counts, unparseable price cells, or malformed signal emojis are dropped with no log entry. After editing a stock `.md`, sanity-check the level appears in startup output before assuming it's armed.
- NSE data via yfinance has ~15 min delay (free tier)

## Data source

- **Live price feed (primary):** Zerodha Kite Connect WebSocket — real-time ticks via `kiteconnect` + `pyotp` (TOTP auto-login). Active when the `ZERODHA_*` vars are set in `.env`. Selected by `price_feed.create_price_feed()`; see `docs/readme.ZERODHA_API.md`.
- **Fallback:** `yfinance`, symbol format `TICKER.NS` (e.g. `RELIANCE.NS`). ~15-min delay on the free tier; used automatically when Kite is unconfigured or unavailable.
- **Poll interval:** ~10 seconds with the Kite WebSocket active; 3-5 minutes on the yfinance fallback.
- **NSE macro feeds:** `nselib` (PyPI; pinned `>=2.5.1` in `requirements.txt`). Used by `fetch_fii_dii.py` for daily FII/DII flows via `capital_market.capital_market_data.fii_dii_trading_activity()` — hits NSE's API endpoint with a primed-cookie session. More resilient against Akamai than raw Playwright, but not guaranteed; a deprecated Playwright fallback in `browser_utils.scrape_nse_fii_dii` is kept for one release.
- **Market hours:** 9:15 AM to 3:30 PM IST, Monday to Friday.
