"""
Main scheduler and orchestrator.

Flow:
  1. Validate credentials, load stock configs, send startup ping.
  2. Every POLL_INTERVAL_SECONDS:
     a. If market is closed → sleep.
     b. Fetch current prices for all tickers.
     c. Check price-level crossings → send alerts.
     d. Once per trading day at open → check calendar alerts + reload configs.
  3. Persist state to disk after every poll.
"""
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pytz
import yfinance as yf

from .caldav_sync import start_radicale, sync_calendar
from .config import (
    DISCORD_BROADCAST_CHANNEL,
    STOCKS_DIR, EXCLUDED_MD_FILES,
    POLL_INTERVAL_SECONDS, COOLDOWN_MINUTES, SPECIAL_ALERT_COOLDOWN_DAYS,
    MARKET_OPEN, MARKET_CLOSE, MARKET_TIMEZONE,
    STATE_FILE, ALERTS_LOG, CUSTOM_ALERTS_FILE,
    SENT_ALERTS_FILE, FEEDBACK_LOG, CONVERSATIONS_LOG,
    CALDAV_INI, CALDAV_STORAGE_DIR,
    APPROACH_DEAD_ZONE_PCT, APPROACH_MAX_RECENT_ALERTS,
    APPROACH_ATR_MULTIPLIER, APPROACH_COOLDOWN_HOURS,
    GOLD_CONFIG_FILE,
    NIGHTLY_REFRESH_FILE, NIGHTLY_REFRESH_HOUR, NIGHTLY_REFRESH_MODE,
    REQUESTS_DIR,
    POLL_INTERVAL_FALLBACK_SECONDS,
)
from .price_feed import PriceFeed, create_price_feed
from .parser import load_all_stocks, StockConfig, parse_gold_file, GoldConfig
from .engine import AlertEngine, FiredAlert
from .discord_notifier import DiscordNotifier
from .state import BotState
from .custom_alerts import CustomAlertsStore
from .floor_context import _load_ohlc, _compute_atr
from .trade_levels import live_overlay
from .regime_context import compute_all_regimes, format_regime_transition
from .confidence import compute_confidence, format_stats_line, dma_hint
from .portfolio_context import portfolio_fragment
from .forecaster import prophet_forecast
from .mmi import fetch_mmi, format_telegram as format_mmi_telegram, format_pin as format_mmi_pin
from .feedback import SentAlertsRegistry, FeedbackStore, ConversationStore
from . import discord_client
from .digest import build_sector_lookup
from . import gold as gold_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

IST = pytz.timezone(MARKET_TIMEZONE)

# Module-level price feed handle. Initialised to None at startup; wired inside
# run() via create_price_feed(). _fetch_one() checks this first before falling
# back to yfinance.
_price_feed: Optional[PriceFeed] = None


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() > 4:  # 5=Saturday, 6=Sunday
        return False
    open_dt = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_dt = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_dt <= now <= close_dt


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def fetch_prices(stocks: list[StockConfig]) -> dict[str, float]:
    """
    Returns {ticker: price} for each stock where a price was retrievable.
    Uses fast_info (single lightweight API call per ticker) with a
    history() fallback. Failures are logged but never crash the loop.
    """
    prices = {}
    for stock in stocks:
        price = _fetch_one(stock.yf_symbol)
        if price is not None:
            prices[stock.ticker] = price
        else:
            logger.warning(f"Could not fetch price for {stock.ticker} ({stock.yf_symbol})")
    return prices


def _fetch_one(yf_symbol: str) -> Optional[float]:
    # Primary path: real-time price feed (no rate-limit).
    # Falls through to yfinance when feed is not configured or returns None
    # (e.g. outside market hours, instrument not subscribed).
    if _price_feed is not None:
        price = _price_feed.get_price(yf_symbol)
        if price is not None:
            return price

    # Fallback: yfinance — fast_info first, then last 1-min candle.
    # Ticker construction is separated so a fast_info failure doesn't skip history().
    try:
        t = yf.Ticker(yf_symbol)
    except Exception:
        return None

    try:
        price = t.fast_info.last_price
        if price and price > 0:
            return float(price)
    except Exception:
        pass

    try:
        hist = t.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug(f"yfinance fallback failed for {yf_symbol}: {e}")

    return None


# ---------------------------------------------------------------------------
# MMI update helper
# ---------------------------------------------------------------------------

def _apply_mmi_update(
    snap,
    notifier: "DiscordNotifier",
    state: "BotState",
    notify: bool,
    prev_value: Optional[float],
) -> None:
    """
    Central MMI update handler. Called on startup and every poll.

    compact mode:
      - Always silently edits the pinned message with the latest value.
      - Sends a notification only when notify=True (zone change or restart).
    full mode:
      - Sends full message + pins it only when notify=True.
    """
    if state.mmi_pin_mode == "compact":
        pin_text = format_mmi_pin(snap)
        if state.mmi_pinned_message_id:
            ok = notifier.edit_message(state.mmi_pinned_message_id, pin_text)
            if not ok:
                # Edit failed (message deleted, too old, etc.) — drop the stale ID
                # and fall through to create a fresh pin below
                state.mmi_pinned_message_id = None
        if not state.mmi_pinned_message_id:
            mid = notifier.send(pin_text)
            if mid:
                notifier.pin_message(mid)
                state.mmi_pinned_message_id = mid
        if notify:
            notifier.send(format_mmi_telegram(snap, prev_value))
    else:  # full mode
        if notify:
            msg = format_mmi_telegram(snap, prev_value)
            mid = notifier.send(msg)
            if mid:
                notifier.pin_message(mid)


# ---------------------------------------------------------------------------
# OHLC bootstrap (ensures every stock has enough bars for forecasting)
# ---------------------------------------------------------------------------

def _bootstrap_ohlc_cache(stocks: list[StockConfig]) -> None:
    """
    Ensure every stock has at least 2 years of OHLC data cached before
    forecast and regime computations run. Only fetches when the cache is
    absent or has fewer than 120 bars (Prophet's minimum).
    Uses load_ohlc_cached() from ohlc_cache.py — incremental, never re-downloads
    bars already on disk.
    """
    from .ohlc_cache import load_ohlc_cached, read_ohlc_cache
    needs_fetch = []
    for stock in stocks:
        df = read_ohlc_cache(stock.ticker)
        if df is None or len(df) < 120:
            needs_fetch.append(stock)

    if not needs_fetch:
        logger.info("OHLC bootstrap: all caches sufficient, nothing to fetch.")
        return

    logger.info(f"OHLC bootstrap: fetching data for {len(needs_fetch)} stock(s) with thin/missing caches…")
    for i, stock in enumerate(needs_fetch, 1):
        logger.info(f"  [{i}/{len(needs_fetch)}] {stock.ticker}: fetching 2y history…")
        try:
            df = load_ohlc_cached(stock.ticker, stock.yf_symbol, period="2y")
            logger.info(f"  [{i}/{len(needs_fetch)}] {stock.ticker}: {len(df)} bars cached.")
        except Exception as e:
            logger.warning(f"  [{i}/{len(needs_fetch)}] {stock.ticker}: fetch failed — {e}")
    logger.info("OHLC bootstrap: done.")


# ---------------------------------------------------------------------------
# Forecast refresh (runs daily at market open)
# ---------------------------------------------------------------------------

def _refresh_forecasts(stocks: list[StockConfig]) -> dict[str, dict]:
    """
    Run Prophet forecast for all stocks that have enough OHLC data.
    Returns {ticker: {30: {lower, predicted, upper, trend}, 60: ..., 90: ...}}.
    Runs in a background thread to avoid blocking the poll loop.
    """
    forecasts = {}
    total = len(stocks)
    logger.info(f"Forecasts: computing Prophet 30/60/90d for {total} stocks "
                f"(this takes ~1-2 min)…")
    for i, stock in enumerate(stocks, 1):
        df = _load_ohlc(stock.ticker)
        if df is None or len(df) < 120:
            logger.info(f"  [{i}/{total}] {stock.ticker}: skipped (insufficient OHLC)")
            continue
        logger.info(f"  [{i}/{total}] {stock.ticker}: forecasting…")
        try:
            fc = prophet_forecast(df["Close"])
            if fc:
                forecasts[stock.ticker] = fc
                logger.info(f"  [{i}/{total}] {stock.ticker}: 30d={fc[30]['predicted']:.0f} "
                          f"({fc[30]['lower']:.0f}-{fc[30]['upper']:.0f})")
        except Exception as e:
            logger.warning(f"  [{i}/{total}] {stock.ticker}: forecast failed — {e}")
    logger.info(f"Forecasts: done ({len(forecasts)}/{total} produced).")
    return forecasts


# ---------------------------------------------------------------------------
# Approach alerts helpers
# ---------------------------------------------------------------------------

def _compute_stock_atrs(stocks: list[StockConfig]) -> dict[str, float]:
    """
    Compute 14-period ATR for each stock from local OHLC cache.
    No network calls — uses the CSV files in analysis/.
    """
    atrs = {}
    for stock in stocks:
        df = _load_ohlc(stock.ticker)
        if df is not None and len(df) >= 20:
            try:
                atrs[stock.ticker] = _compute_atr(df)
            except Exception as e:
                logger.debug(f"ATR computation failed for {stock.ticker}: {e}")
    return atrs


def _compute_stock_regimes(
    stocks: list[StockConfig],
    state: BotState,
    notifier: DiscordNotifier,
) -> dict[str, dict]:
    """
    Run HMM regime detection + MC for all stocks.  Detect regime transitions
    vs the last saved regime and fire Telegram alerts for any changes.

    Called once daily at market open (mirrors gold regime pattern).
    Returns the regime cache dict for use during the poll loop.
    """
    logger.info(f"Regimes: running HMM + Monte Carlo for {len(stocks)} stocks…")
    regime_cache = compute_all_regimes(stocks)
    logger.info(f"Regimes: done ({len(regime_cache)} computed).")

    # Detect transitions (same pattern as gold: main.py gold_last_regime)
    for ticker, data in regime_cache.items():
        new_regime = data["current"]
        prev_regime = state.stock_regimes.get(ticker)

        if prev_regime is not None and prev_regime != new_regime:
            body = format_regime_transition(
                ticker,
                prev_regime,
                new_regime,
                data["confidence"],
                data["mc_median_30d"],
                data["last_close"],
            )
            notifier.send(body)
            logger.info(f"Regime: {ticker} {prev_regime} → {new_regime}")

        # Update state with the current regime for next comparison
        state.stock_regimes[ticker] = new_regime
        # Push confidence reading so _regime_header() can compute direction arrows
        state.push_regime_prob(ticker, data.get("confidence", 0.0))

    state.regime_scan_date = datetime.now(IST).date()
    state.save()

    return regime_cache


def _count_recent_alerts(days: int = 30) -> dict[str, int]:
    """
    Count alerts per ticker from alerts.jsonl in the last N days.
    Lightweight scan — reads only the timestamp and ticker fields.
    """
    from collections import Counter
    cutoff = datetime.now(IST) - __import__("datetime").timedelta(days=days)
    counts: Counter = Counter()
    if not ALERTS_LOG.exists():
        return dict(counts)
    try:
        with ALERTS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    ts = d.get("ts", "")
                    if ts:
                        # Parse ISO timestamp and compare
                        alert_dt = datetime.fromisoformat(ts)
                        if alert_dt.tzinfo is None:
                            alert_dt = IST.localize(alert_dt)
                        if alert_dt >= cutoff:
                            counts[d.get("ticker", "")] += 1
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError as e:
        logger.debug(f"Could not read alerts log for counting: {e}")
    return dict(counts)


def _refresh_nifty_cache() -> None:
    """Refresh the Nifty 50 OHLC cache (used for relative strength scoring)."""
    nifty_path = Path("analysis/NIFTY50_ohlc_cache.csv")
    try:
        nifty = yf.download("^NSEI", period="2y", progress=False)
        if nifty is not None and len(nifty) > 0:
            if hasattr(nifty.columns, "levels"):
                nifty.columns = nifty.columns.get_level_values(0)
            nifty.to_csv(nifty_path)
            logger.info(f"Nifty cache refreshed: {len(nifty)} rows")
        else:
            logger.warning("Nifty cache refresh: no data returned")
    except Exception as e:
        logger.warning(f"Nifty cache refresh failed: {e}")


# ---------------------------------------------------------------------------
# Nightly refresh: auto-generate analysis requests after each trading day
# ---------------------------------------------------------------------------

def _generate_nightly_requests() -> list[str]:
    """
    Read config/active_refresh_stocks.txt and create a request JSON in
    requests/ for each ticker that doesn't already have one pending.
    Returns list of tickers for which requests were created.
    """
    if not NIGHTLY_REFRESH_FILE.exists():
        logger.info("Nightly refresh: config file not found, skipping.")
        return []

    # Read tickers from config (skip comments and blank lines)
    tickers = []
    for line in NIGHTLY_REFRESH_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            tickers.append(line.upper())

    if not tickers:
        return []

    # Check which tickers already have a pending request
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    pending = set()
    for f in REQUESTS_DIR.glob("*.json"):
        if f.name == ".gitkeep":
            continue
        try:
            req = json.loads(f.read_text())
            pending.add(req.get("ticker", "").upper())
        except (json.JSONDecodeError, OSError):
            pass

    created = []
    for ticker in tickers:
        if ticker in pending:
            logger.info(f"Nightly refresh: {ticker} already has a pending request, skipping.")
            continue
        req_path = REQUESTS_DIR / f"{ticker}_nightly.json"
        req_path.write_text(json.dumps({
            "ticker": ticker,
            "mode": NIGHTLY_REFRESH_MODE,
            "update": True,
        }, indent=2))
        created.append(ticker)

    return created


# ---------------------------------------------------------------------------
# Hot-reload: detect stock file changes between polls
# ---------------------------------------------------------------------------

def _stocks_dir_mtime(stocks_dir: Path) -> float:
    """
    Return the newest mtime across all .md files in stocks/.
    Used to detect when an analysis run creates or updates a stock file,
    so the bot can reload configs immediately instead of waiting for
    the next day's market open.
    """
    newest = 0.0
    for md in stocks_dir.glob("*.md"):
        try:
            mt = md.stat().st_mtime
            if mt > newest:
                newest = mt
        except OSError:
            pass
    return newest


# ---------------------------------------------------------------------------
# Gold daily check (runs OUTSIDE market hours — gold is global)
# ---------------------------------------------------------------------------

def _run_gold_daily_check(
    cfg: GoldConfig,
    state: BotState,
    notifier: DiscordNotifier,
) -> None:
    """
    Daily gold pipeline:
      1. Fetch all 7 yfinance series, compute the bundle, write JSON files
      2. Detect regime transitions vs the saved last regime
      3. Detect zone crossings vs the saved last 24K ₹/gram price
      4. Fire Telegram on transitions / crossings
      5. Persist new state

    Read commodities/metals_instructions.md before changing this — the
    discipline (one writer per file, no daily noise, etc.) is documented there.
    """
    try:
        bundle = gold_module.fetch_gold_snapshot(cfg)
    except Exception as e:
        logger.error(f"Gold daily check failed: {e}", exc_info=True)
        return

    physical = bundle.get("prices", {}).get("physical_24k_inr_per_gram", {}).get("value")
    new_regime = bundle.get("regime", {}).get("current")

    if physical is None or new_regime is None:
        logger.warning("Gold: incomplete bundle — skipping alert dispatch")
        return

    # ---- Regime transition detection ----
    # Source-of-truth: compare to the previous regime persisted in state.
    # The bundle's own transition_today flag (computed from 30D history) is
    # used as a tiebreaker but state.gold_last_regime is authoritative across
    # restarts.
    prev_regime = state.gold_last_regime
    transition = prev_regime is not None and prev_regime != new_regime

    if transition:
        narrative = gold_module.load_gold_narrative()
        # Patch the bundle so format_gold_telegram emits the saved-state
        # transition rather than the bundle's history-based one.
        bundle["regime"]["previous"] = prev_regime
        bundle["regime"]["transition_today"] = True
        body = gold_module.format_gold_telegram(
            bundle, narrative_quotes=narrative, transition_kind="regime",
        )
        notifier.send(body)
        logger.info(f"Gold: regime transition {prev_regime} → {new_regime} (Telegram sent)")

    # ---- Zone crossing detection ----
    crossings = gold_module.check_gold_zones(
        cfg,
        prev_inr_per_gram=state.gold_last_inr_per_gram,
        curr_inr_per_gram=physical,
    )
    for zone, message in crossings:
        # Reuse the level-cooldown machinery so a zone can't fire twice on
        # consecutive daily ticks if price oscillates around the boundary.
        key = state.level_key("GOLD", zone.price_str)
        if not state.level_cooled_down(key, cooldown_minutes=60 * 24):
            continue
        narrative = gold_module.load_gold_narrative()
        body = (
            gold_module.format_gold_telegram(
                bundle, narrative_quotes=narrative, transition_kind="zone",
            )
            + "\n\n"
            + f"⚡ ZONE: {zone.signal} {zone.alert_type} {zone.price_str}/g — {message}"
        )
        notifier.send(body)
        state.mark_level_fired(key, price=physical)
        logger.info(f"Gold: zone crossed {zone.price_str} ({message})")

    # ---- Persist new state ----
    state.gold_last_inr_per_gram = physical
    state.gold_last_regime = new_regime
    state.gold_last_check_date = datetime.now(IST).date()
    state.save()


def _run_gold_weekly_digest(
    cfg: GoldConfig,
    state: BotState,
    notifier: DiscordNotifier,
) -> None:
    """
    Sunday weekly digest — fires the full bundle as a recap regardless of
    transitions. Called separately from the daily check so a Sunday with
    a real transition still gets exactly one message (the digest).
    """
    try:
        bundle = gold_module.fetch_gold_snapshot(cfg)
        narrative = gold_module.load_gold_narrative()
        body = gold_module.format_gold_telegram(
            bundle, narrative_quotes=narrative, transition_kind="weekly",
        )
        notifier.send(body)
        logger.info("Gold: Sunday weekly digest sent")

        # Sync persisted state from this run too
        physical = bundle.get("prices", {}).get("physical_24k_inr_per_gram", {}).get("value")
        new_regime = bundle.get("regime", {}).get("current")
        if physical is not None:
            state.gold_last_inr_per_gram = physical
        if new_regime is not None:
            state.gold_last_regime = new_regime
        state.gold_last_check_date = datetime.now(IST).date()
        state.save()
    except Exception as e:
        logger.error(f"Gold weekly digest failed: {e}", exc_info=True)


def _run_news_scrape() -> None:
    """
    Daily news scrape — fetch RSS headlines, diff against seen set,
    extract tickers/themes, generate templated causal chains.
    Zero token cost (pure Python). Runs after market close.
    """
    try:
        from news_scraper import scrape
        n = scrape()
        logger.info(f"News scrape: {n} new signals")
    except Exception as e:
        logger.error(f"News scrape failed: {e}", exc_info=True)


def _run_discovery_digest(notifier) -> None:
    """
    Sunday weekly discovery digest — scans explore_candidates from insider
    data, scores them on smart money + macro + technicals, sends the top
    candidates as a Telegram watchlist.
    """
    try:
        from discovery_scanner import scan, format_telegram_digest
        candidates = scan()
        if candidates:
            body = format_telegram_digest(candidates)
            notifier.send(body)
            logger.info(f"Discovery: weekly digest sent ({len(candidates)} candidates)")
        else:
            logger.info("Discovery: no candidates to report")
    except Exception as e:
        logger.error(f"Discovery digest failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Alert body assembly helpers
# ---------------------------------------------------------------------------

def _regime_header(ticker: str, regime_cache: dict, state: "BotState") -> str:
    """
    Build the regime portion of the alert header: "Bull 87% →"
    Arrow shows 3-reading trend in regime confidence: ↑ strengthening, ↓ weakening, → stable.
    """
    data = regime_cache.get(ticker, {})
    regime = data.get("current", "unknown").capitalize()
    pct = int(data.get("confidence", 0.0) * 100)
    hist = state.regime_prob_history.get(ticker, [])
    if len(hist) >= 2:
        delta = hist[-1] - hist[-2]
        arrow = "↑" if delta > 0.05 else "↓" if delta < -0.05 else "→"
    else:
        arrow = "→"
    return f"{regime} {pct}% {arrow}"


def _sizing_hint(verdict: str, regime: str, alert_type: str) -> str:
    """
    Produce an actionable size word for line 3 of the alert.
    SELL: trim percentage.  BUY/WATCH: add percentage scaled by verdict + regime.
    """
    if alert_type == "SELL":
        if verdict in ("STRONG SELL", "CONFIRMED SELL"):
            return "Trim 30%"
        elif verdict == "MODERATE SELL":
            return "Trim 15%"
        else:
            return "Trim 10%"

    base = {"HIGH CONVICTION": 2.5, "MODERATE": 1.5, "BUILDING": 1.0}.get(verdict, 0.5)
    regime_mult = {"bull": 1.0, "sideways": 0.5}.get(regime.lower(), 0.25)
    pct = base * regime_mult
    if pct < 0.5:
        return "Watch only"
    return f"Add {pct:.1f}%".replace(".0%", "%")


# Display tier word for the header — one strength axis, paired with conf.emoji
# (🟢🟡🟠🔴). The verdict strings themselves are unchanged (they feed _sizing_hint).
_VERDICT_TIER = {
    "HIGH CONVICTION": "HIGH", "MODERATE": "MODERATE", "BUILDING": "BUILDING", "WEAK": "WEAK",
    "STRONG SELL": "STRONG", "CONFIRMED SELL": "CONFIRMED", "MODERATE SELL": "MODERATE",
    "WEAK SELL": "WEAK",
}


def _money(p: float) -> str:
    """₹-amount without trailing .00 (613.0→'613', 33.45→'33.45')."""
    return f"{p:,.2f}".rstrip("0").rstrip(".")


def _compose_alert_body(alert, curr_price, active_regimes, state, stocks_by_ticker):
    """
    Build the full Telegram alert body: 3-line core + floor/portfolio enrichment
    + optional Sable opinion + stats. Single source of truth for both the
    claude_alerts and manual_alerts dispatch loops (they differ only in the
    sent-alert source tag, not the body).

    Returns (body, conf): the composed string plus the ConfidenceResult (or None
    if confidence failed). The caller logs `conf`'s factor vector into the sent-
    alerts registry — the calibration spine's ground-truth record.

    Confidence is wrapped so a crash can never block the core alert.
    """
    conf = None
    try:
        conf = compute_confidence(
            alert.alert_type, alert.ticker, alert.price_str,
            curr_price, active_regimes, state.mmi_last_value,
        )
    except Exception:
        logger.exception(f"confidence failed for {alert.ticker}")

    stock = stocks_by_ticker.get(alert.ticker)
    regime_hdr = _regime_header(alert.ticker, active_regimes, state)
    regime_str = active_regimes.get(alert.ticker, {}).get("current", "")

    # Regime-gated tactical overlay from the single trade_levels engine (bean 96ic).
    # tl is None ⇒ weak swing OR hostile regime ⇒ no tactical line: the alert still
    # fires as a long-term investment signal and the regime stays visible in line 1.
    tl = None
    try:
        tl = live_overlay(alert.ticker, alert.price_str,
                          regime_cache=active_regimes, stock=stock)
    except Exception:
        logger.exception(f"live_overlay failed for {alert.ticker}")

    verdict = conf.verdict if conf else "BUILDING"
    sizing = _sizing_hint(verdict, regime_str, alert.alert_type)
    emoji = conf.emoji if conf else "⚪"
    tier = _VERDICT_TIER.get(verdict, "") if conf else ""

    # Header: {emoji} {TIER} · <b>ACTION TICKER</b> @ ₹price
    tier_prefix = f"{tier} · " if tier else ""
    header = (f"{emoji} {tier_prefix}<b>{alert.alert_type} {alert.ticker}</b> "
              f"@ ₹{_money(curr_price)}")

    # 🎯 trade line: sizing + tactical tail only when the overlay is present (96ic)
    trade = sizing
    if tl is not None:
        if alert.alert_type == "SELL":
            if tl.reload_to is not None:
                trade += f" → reload ₹{tl.reload_to:,.0f}"
        elif tl.target is not None:
            trade += f" → ₹{tl.target:,.0f}"        # approximate levels → whole rupees
            if tl.stop is not None:
                trade += f" · 🛑 ₹{tl.stop:,.0f}"
            if tl.rr is not None:
                trade += f" · R:R {tl.rr:.1f}"

    thesis = conf.thesis if conf and conf.thesis else alert.message
    parts = [header, f"📍 {regime_hdr}", f"💭 {thesis}", f"🎯 {trade}"]

    # 📈 optional DMA-defence line (only when price hugs the 200-DMA)
    try:
        dh = dma_hint(alert.ticker, curr_price)
        if dh:
            parts.append(f"📈 {dh}")
    except Exception:
        logger.exception(f"dma_hint failed for {alert.ticker}")

    # 📊 merged context line: position (compact) + backtest stats
    ctx: list[str] = []
    try:
        pf = portfolio_fragment(alert.ticker, curr_price)
        if pf:
            ctx.append(pf)
    except Exception:
        logger.exception(f"portfolio_fragment failed for {alert.ticker}")
    if conf:
        try:
            s = format_stats_line(conf)
            if s:
                ctx.append(s)
        except Exception:
            logger.exception(f"stats line failed for {alert.ticker}")
    if ctx:
        parts.append(f"📊 {' · '.join(ctx)}")

    if conf and conf.sable_opinion:
        parts.append(f"<i>Sable — {conf.sable_opinion}</i>")

    return "\n".join(parts), conf


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    if not DISCORD_BROADCAST_CHANNEL:
        raise SystemExit(
            "Missing Discord config.\n"
            "Copy .env.example → .env and fill in DISCORD_BOT_TOKEN, "
            "DISCORD_BROADCAST_CHANNEL and DISCORD_CHAT_CHANNEL."
        )

    # One Discord gateway connection serves ingest + commands + reactions + alerts.
    # Start it first and wait until it's connected, then bind the notifier to its loop.
    client, loop = discord_client.start_and_wait()
    notifier = DiscordNotifier(client, loop, DISCORD_BROADCAST_CHANNEL)

    state = BotState(STATE_FILE, IST)
    engine = AlertEngine(
        state=state,
        tz=IST,
        cooldown_minutes=COOLDOWN_MINUTES,
        special_cooldown_days=SPECIAL_ALERT_COOLDOWN_DAYS,
    )
    custom_store = CustomAlertsStore(CUSTOM_ALERTS_FILE)
    sent_registry = SentAlertsRegistry(SENT_ALERTS_FILE)
    feedback_store = FeedbackStore(FEEDBACK_LOG)
    convo_store = ConversationStore(CONVERSATIONS_LOG)

    # Activate command + reaction dispatch now that the stores exist. The same
    # client already routes ingest channels; Sable answers commands/replies in
    # whichever channel they arrive (channel-local reply rule).
    discord_client.configure(
        notifier=notifier, state=state, custom_store=custom_store,
        sent_registry=sent_registry, feedback_store=feedback_store,
        convo_store=convo_store,
    )

    logger.info("Starting stock alert bot...")
    stocks = _load(STOCKS_DIR, EXCLUDED_MD_FILES)

    # Initialize real-time price feed (no-op if no broker credentials in .env)
    global _price_feed
    try:
        _feed = create_price_feed([s.yf_symbol for s in stocks])
        if _feed is not None:
            _feed.start()
            _price_feed = _feed
            logger.info("Price feed started (%d symbols)", len(stocks))
        else:
            logger.info("No broker credentials — running in yfinance-only mode")
    except Exception as _exc:
        logger.warning("Price feed startup failed; falling back to yfinance: %s", _exc)
        _price_feed = None

    last_stocks_mtime = _stocks_dir_mtime(STOCKS_DIR)

    # Sync README.md Active stocks table from stocks/*.md (idempotent — only writes
    # if drift detected). Non-fatal: log and continue if anything goes wrong.
    try:
        from . import portfolio as _portfolio_module
        _sync_result = _portfolio_module.sync_active_stocks_table()
        if _sync_result.get("ok"):
            logger.info(f"README.md Active stocks synced: {_sync_result.get('count')} stocks.")
        else:
            logger.warning(f"README.md sync skipped: {_sync_result.get('error')}")
    except Exception as _exc:
        logger.warning(f"README.md sync failed (non-fatal): {_exc}")

    # Start CalDAV server and sync calendar events from stock configs
    start_radicale(CALDAV_INI)
    sync_calendar(stocks, CALDAV_STORAGE_DIR)
    logger.info("CalDAV: Radicale started, calendar synced.")

    # Build sector lookup for digest keyword matching (tiny JSON file)
    build_sector_lookup(STOCKS_DIR)

    # Bootstrap OHLC caches (fetches history for new/thin stocks, incremental for rest)
    _bootstrap_ohlc_cache(stocks)

    # Refresh Prophet forecasts for all stocks (daily, non-blocking)
    active_forecasts: dict[str, dict] = _refresh_forecasts(stocks)

    # Compute ATRs and recent alert counts for approach alerts
    active_atrs: dict[str, float] = _compute_stock_atrs(stocks)
    recent_alert_counts: dict[str, int] = _count_recent_alerts(days=30)
    logger.info(f"Approach alerts: ATRs for {len(active_atrs)} stocks, "
                f"recent counts: {recent_alert_counts}")

    # HMM regime detection + Monte Carlo for all stocks (daily)
    # On startup, run the full scan so alerts have regime context immediately.
    active_regimes: dict[str, dict] = _compute_stock_regimes(
        stocks, state, notifier,
    )

    # Fetch current prices and send startup summary
    startup_prices = fetch_prices(stocks)
    notifier.send(_startup_message(stocks, startup_prices))

    # Fetch MMI, send notification, update pin, set as baseline
    snap = fetch_mmi()
    if snap:
        state.update_mmi_baseline(snap.value, snap.zone)
        logger.info(f"MMI baseline: {snap.value:.1f} — {snap.zone}")
        _apply_mmi_update(snap, notifier, state, notify=True, prev_value=None)
        state.save()  # save after apply so mmi_pinned_message_id is persisted

    last_trading_date: Optional[date] = None
    last_close_notified_date: Optional[date] = None
    last_nightly_refresh_date: Optional[date] = None
    last_gold_check_date: Optional[date] = state.gold_last_check_date
    last_gold_weekly_date: Optional[date] = None
    last_discovery_date: Optional[date] = None

    # Load gold config once at startup; reload daily inside the loop
    gold_cfg: Optional[GoldConfig] = parse_gold_file(GOLD_CONFIG_FILE)
    if gold_cfg is not None:
        logger.info(
            f"Gold tracker enabled: target {gold_cfg.target_allocation_pct}%, "
            f"{len(gold_cfg.zones)} zones, {len(gold_cfg.festivals)} festivals"
        )
    else:
        logger.info("Gold tracker disabled (no commodities/gold.md)")

    while True:
        try:
            now = datetime.now(IST)

            # ---------------------------------------------------------------
            # Gold daily check — runs OUTSIDE the market-open gate so it
            # works on weekends and NSE holidays. Gold is global, and
            # holidays are when context matters most.
            # ---------------------------------------------------------------
            if gold_cfg is not None and last_gold_check_date != now.date():
                # Reload the config in case the user edited zones/multiplier
                fresh_cfg = parse_gold_file(GOLD_CONFIG_FILE)
                if fresh_cfg is not None:
                    gold_cfg = fresh_cfg
                _run_gold_daily_check(gold_cfg, state, notifier)
                last_gold_check_date = now.date()

                # Sunday weekly digest — fired in addition to (not instead of)
                # the daily check, but only once per Sunday
                if now.weekday() == 6 and last_gold_weekly_date != now.date():
                    _run_gold_weekly_digest(gold_cfg, state, notifier)
                    last_gold_weekly_date = now.date()

            # ---------------------------------------------------------------
            # Sunday discovery digest — weekly multibagger watchlist.
            # Runs outside market-open gate (same as gold) so it fires on
            # Sundays regardless of market hours.
            # ---------------------------------------------------------------
            if now.weekday() == 6 and last_discovery_date != now.date():
                _run_discovery_digest(notifier)
                last_discovery_date = now.date()

            # ---------------------------------------------------------------
            # Daily reload: runs once at or after market open each new day
            # ---------------------------------------------------------------
            if is_market_open() and last_trading_date != now.date():
                last_trading_date = now.date()
                stocks = _load(STOCKS_DIR, EXCLUDED_MD_FILES)
                sync_calendar(stocks, CALDAV_STORAGE_DIR)  # reflect any markdown edits

                notifier.send("🔔  MARKET OPEN  NSE is open — 9:15 AM IST")

                # Calendar alerts (e.g. "SUVEN May data approaching — April warning")
                cal_alerts = engine.check_calendar_alerts(stocks)
                if cal_alerts:
                    notifier.send_many([f"📅  REMINDER  {m}" for m in cal_alerts])
                    state.save()

                # Bootstrap OHLC for any newly added stocks
                _bootstrap_ohlc_cache(stocks)

                # Daily forecast refresh (Prophet 30/60/90 day predictions)
                active_forecasts = _refresh_forecasts(stocks)

                # Refresh ATRs and alert counts for approach alerts
                active_atrs = _compute_stock_atrs(stocks)
                recent_alert_counts = _count_recent_alerts(days=30)

                # Daily HMM regime scan + MC simulation for all stocks
                active_regimes = _compute_stock_regimes(
                    stocks, state, notifier,
                )

                # Refresh Nifty 50 OHLC cache for relative strength scoring
                _refresh_nifty_cache()

                # Re-subscribe with today's stock list (handles daily token renewal internally)
                try:
                    if _price_feed is not None:
                        _price_feed.refresh_subscriptions([s.yf_symbol for s in stocks])
                    else:
                        # Feed was None at startup; attempt recovery now
                        _feed = create_price_feed([s.yf_symbol for s in stocks])
                        if _feed is not None:
                            _feed.start()
                            _price_feed = _feed
                            logger.info("Price feed recovered at market open")
                except Exception as _exc:
                    logger.warning("Price feed refresh failed; continuing on yfinance: %s", _exc)

                last_stocks_mtime = _stocks_dir_mtime(STOCKS_DIR)

            # ---------------------------------------------------------------
            # Hot-reload: pick up new/changed stock files immediately
            # (e.g. after an autonomous analysis run creates a new stock)
            # ---------------------------------------------------------------
            current_mtime = _stocks_dir_mtime(STOCKS_DIR)
            if current_mtime > last_stocks_mtime:
                old_count = len(stocks)
                stocks = _load(STOCKS_DIR, EXCLUDED_MD_FILES)
                sync_calendar(stocks, CALDAV_STORAGE_DIR)
                active_atrs = _compute_stock_atrs(stocks)
                recent_alert_counts = _count_recent_alerts(days=30)
                last_stocks_mtime = current_mtime
                new_tickers = {s.ticker for s in stocks}
                logger.info(f"Hot-reload: {old_count} → {len(stocks)} stocks")
                if len(stocks) != old_count:
                    notifier.send(
                        f"🔄  CONFIG RELOAD  Stock files changed — "
                        f"now tracking {len(stocks)} stocks: "
                        f"{', '.join(sorted(new_tickers))}"
                    )

            # ---------------------------------------------------------------
            # Price polling
            # ---------------------------------------------------------------
            if is_market_open():
                prices = fetch_prices(stocks)

                if not prices:
                    # No prices returned — likely a market holiday
                    logger.info("No price data returned — possible NSE holiday, skipping.")
                else:
                    logger.info("Prices: " + ", ".join(
                        f"{t}=₹{p:.2f}" for t, p in sorted(prices.items())
                    ))
                    claude_alerts = (
                        engine.check_prices(stocks, prices, atrs=active_atrs)
                        if state.alert_mode in ("claude", "both") else []
                    )
                    manual_alerts = (
                        engine.check_custom_alerts(custom_store, prices)
                        if state.alert_mode in ("manual", "both") else []
                    )
                    all_alerts = claude_alerts + manual_alerts
                    if all_alerts:
                        _log_alerts(all_alerts, prices)
                        now_ist = datetime.now(IST)
                        stocks_by_ticker = {s.ticker: s for s in stocks}
                        for a in claude_alerts:
                            curr_price = prices.get(a.ticker, 0.0)
                            body, conf = _compose_alert_body(
                                a, curr_price, active_regimes, state, stocks_by_ticker
                            )
                            mid = notifier.send(body)
                            if mid:
                                sent_registry.register(
                                    mid, a.ticker, a.alert_type, a.price_str,
                                    curr_price, a.signal, a.confidence,
                                    a.message, "claude", now_ist,
                                    confidence_result=conf,
                                )
                            time.sleep(0.5)
                        for a in manual_alerts:
                            curr_price = prices.get(a.ticker, 0.0)
                            body, conf = _compose_alert_body(
                                a, curr_price, active_regimes, state, stocks_by_ticker
                            )
                            mid = notifier.send(body)
                            if mid:
                                sent_registry.register(
                                    mid, a.ticker, a.alert_type, a.price_str,
                                    curr_price, a.signal, a.confidence,
                                    a.message, "manual", now_ist,
                                    confidence_result=conf,
                                )
                            time.sleep(0.5)

                # ---------------------------------------------------------------
                # Forecast alerts ("zone approaching" — from daily Prophet)
                # ---------------------------------------------------------------
                if active_forecasts:
                    fc_alerts = engine.check_forecast_alerts(
                        stocks, prices, active_forecasts
                    )
                    if fc_alerts:
                        notifier.send_many(fc_alerts)
                        state.save()

                # ---------------------------------------------------------------
                # Approach alerts (per-stock auto-calibrated proximity nudges)
                # ---------------------------------------------------------------
                if active_atrs:
                    approach_alerts = engine.check_approach_alerts(
                        stocks, prices, active_atrs, recent_alert_counts,
                        dead_zone_threshold_pct=APPROACH_DEAD_ZONE_PCT,
                        max_recent_alerts=APPROACH_MAX_RECENT_ALERTS,
                        atr_multiplier=APPROACH_ATR_MULTIPLIER,
                        cooldown_hours=APPROACH_COOLDOWN_HOURS,
                    )
                    if approach_alerts:
                        notifier.send_many(approach_alerts)
                        state.save()

                # ---------------------------------------------------------------
                # MMI polling
                # ---------------------------------------------------------------
                snap = fetch_mmi()
                if snap:
                    logger.info(f"MMI: {snap.value:.1f} — {snap.zone}")
                    zone_changed = state.should_alert_mmi(snap.value, snap.zone)
                    prev_value = state.mmi_last_value
                    _apply_mmi_update(snap, notifier, state, notify=zone_changed, prev_value=prev_value)
                    if zone_changed:
                        state.mark_mmi_alerted(snap.value, snap.zone)
                    else:
                        state.update_mmi_baseline(snap.value, snap.zone)

                state.save()
            else:
                # Fire close notification once per day (after market was open today)
                if last_trading_date == now.date() and last_close_notified_date != now.date():
                    last_close_notified_date = now.date()
                    notifier.send("🔔  MARKET CLOSED  NSE is closed — 3:30 PM IST")

                    # Daily news scrape — diff-based, zero token cost.
                    # Runs once after market close to catch the day's headlines.
                    _run_news_scrape()

                # Nightly refresh: generate analysis requests at 11 PM on trading days
                if (last_trading_date == now.date()
                        and now.hour >= NIGHTLY_REFRESH_HOUR
                        and last_nightly_refresh_date != now.date()):
                    last_nightly_refresh_date = now.date()

                    # 1. Reconcile fired alerts against portfolio transactions.
                    # Portfolio import (import_portfolio.py) is now loop-driven — it runs
                    # whenever new xlsx files are detected in stock portfolio/, so portfolio.db
                    # is already fresh by the time this block fires.
                    try:
                        import reconcile_portfolio as _rp
                        _summary = _rp.reconcile()
                        _digest_section = _rp.format_digest_section(_summary)
                        if _digest_section:
                            notifier.send(_digest_section)
                            logger.info(f"Reconciliation: {len(_summary)} items")
                    except Exception as _e:
                        logger.error(f"Reconciliation error: {_e}", exc_info=True)

                    # 3. Queue nightly analysis requests
                    created = _generate_nightly_requests()
                    if created:
                        notifier.send(
                            f"🌙  NIGHTLY REFRESH  Queued {len(created)} stocks: "
                            f"{', '.join(created)}"
                        )
                        logger.info(f"Nightly refresh: created requests for {created}")

                logger.debug("Market closed.")

        except KeyboardInterrupt:
            logger.info("Shutting down.")
            if _price_feed is not None:
                _price_feed.stop()
            state.save()
            raise SystemExit(0)
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)

        _poll_interval = POLL_INTERVAL_SECONDS if _price_feed is not None else POLL_INTERVAL_FALLBACK_SECONDS
        try:
            time.sleep(_poll_interval)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            if _price_feed is not None:
                _price_feed.stop()
            state.save()
            raise SystemExit(0)


def _log_alerts(alerts: list[FiredAlert], prices: dict[str, float]) -> None:
    """Append fired alerts to data/alerts.jsonl for the TUI to read."""
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(IST)
    try:
        with ALERTS_LOG.open("a", encoding="utf-8") as f:
            for a in alerts:
                f.write(json.dumps({
                    "ts": now.isoformat(),
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%H:%M:%S"),
                    "ticker": a.ticker,
                    "alert_type": a.alert_type,
                    "price_str": a.price_str,
                    "price": prices.get(a.ticker),
                    "signal": a.signal,
                    "confidence": a.confidence,
                    "message": a.message,
                    "source": a.source,
                }) + "\n")
    except OSError as e:
        logger.error(f"Could not write alert log: {e}")


def _startup_message(stocks: list[StockConfig], prices: dict[str, float]) -> str:
    header = f"{'Ticker':<10} {'Price':>10}"
    divider = "-" * len(header)
    rows = []
    for s in stocks:
        price = prices.get(s.ticker)
        price_str = f"₹{price:,.2f}" if price else "unavailable"
        rows.append(f"{s.ticker:<10} {price_str:>10}")
    table = "\n".join([header, divider] + rows)
    return f"Stock alert bot is now running.\n\n<pre>{table}</pre>"


def _load(stocks_dir, excluded):
    stocks = load_all_stocks(stocks_dir, excluded)
    logger.info(f"Loaded {len(stocks)} stocks: {[s.ticker for s in stocks]}")
    return stocks
