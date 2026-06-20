"""
Command handlers for Sable. Dispatched by the Discord gateway
(alert_bot/discord_client.py), which calls _handle / _handle_reply / log_reaction.

Supported commands: see commands.md in the project root.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import pytz

from .config import (
    REQUESTS_DIR, NIGHTLY_LEVELS_DIR, MARKET_TIMEZONE,
    STOCKS_DIR, EXCLUDED_MD_FILES,
)
from .custom_alerts import CustomAlertsStore, _SIGNAL
from .feedback import SentAlertsRegistry, FeedbackStore, ConversationStore, EMOJI_MEANINGS
from .discord_notifier import DiscordNotifier
from .state import BotState

logger = logging.getLogger(__name__)

_MODE_LABELS = {
    "claude": "Claude 🤖 alerts only",
    "manual": "manual 🐱 alerts only",
    "both":   "Claude 🤖 + manual 🐱 alerts",
}

# Second token is always a verb for these — never treated as a ticker
_RESERVED_VERBS = {"add", "list", "clear", "mode", "delete"}

_VALID_MODES      = {"comprehensive", "full", "chart-news", "chart-news-community", "chart-only", "retrospective"}
_VALID_PERIODS    = {"1y", "2y", "3y", "5y", "max"}
_DEFAULT_PERIOD   = "2y"


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def _handle(
    text: str,
    chat_id: int,
    notifier: DiscordNotifier,
    state: BotState,
    custom_store: CustomAlertsStore,
    feedback_store: FeedbackStore | None = None,
    convo_store: "ConversationStore | None" = None,
) -> None:
    parts = text.strip().split()
    if not parts:
        return

    cmd = parts[0].lower()

    if cmd == "/mmi":
        _handle_mmi(parts, chat_id, notifier, state)
    elif cmd == "/alert":
        _handle_alert(parts, text, chat_id, notifier, state, custom_store)
    elif cmd == "/analyze":
        _handle_analyze(parts, chat_id, notifier)
    elif cmd == "/backtest":
        _handle_backtest(parts, chat_id, notifier)
    elif cmd == "/forecast":
        _handle_forecast(parts, chat_id, notifier)
    elif cmd == "/note":
        _handle_note(parts, text, chat_id, notifier, convo_store)
    elif cmd == "/react":
        _handle_react(parts, chat_id, notifier, feedback_store)
    elif cmd == "/portfolio":
        _handle_portfolio(parts, chat_id, notifier)
    elif cmd == "/help":
        _handle_help(parts, chat_id, notifier)


# ---------------------------------------------------------------------------
# /mmi
# ---------------------------------------------------------------------------

def _handle_mmi(parts, chat_id, notifier, state):
    if len(parts) < 2:
        return
    mode = parts[1].lower()
    if mode == "full":
        state.mmi_pin_mode = "full"
        state.save()
        notifier.reply(chat_id, "✅ MMI pin mode set to <b>full</b> — full message pinned on each zone change.")
        logger.info("MMI pin mode switched to full.")
    elif mode == "compact":
        state.mmi_pin_mode = "compact"
        state.mmi_pinned_message_id = None
        state.save()
        notifier.reply(chat_id, "✅ MMI pin mode set to <b>compact</b> — pinned summary silently updated on each zone change.")
        logger.info("MMI pin mode switched to compact.")
    else:
        notifier.reply(chat_id, "Unknown mode. Use <b>/mmi full</b> or <b>/mmi compact</b>.")


# ---------------------------------------------------------------------------
# /alert — dispatcher
# ---------------------------------------------------------------------------

def _handle_alert(parts, full_text, chat_id, notifier, state, custom_store):
    if len(parts) < 2:
        notifier.reply(chat_id, _help_text())
        return

    verb = parts[1].lower()

    if verb == "mode":
        _alert_mode(parts, chat_id, notifier, state)
    elif verb == "list":
        _alert_list(parts, chat_id, notifier, custom_store)
    elif verb == "clear":
        _alert_clear(parts, chat_id, notifier, custom_store)
    elif verb == "add":
        # Explicit: /alert add TICKER price TYPE ...
        if len(parts) < 3:
            notifier.reply(chat_id, _help_text())
            return
        ticker = parts[2].upper()
        offset = len(parts[0]) + 1 + len(parts[1]) + 1 + len(parts[2]) + 1
        rest = full_text[offset:].strip()
        _alert_add(ticker, rest, chat_id, notifier, custom_store)
    else:
        # Shorthand: /alert TICKER price TYPE ...  (ticker is second token)
        ticker = verb.upper()
        offset = len(parts[0]) + 1 + len(parts[1]) + 1
        rest = full_text[offset:].strip()
        if not rest:
            notifier.reply(chat_id, _help_text())
            return
        _alert_add(ticker, rest, chat_id, notifier, custom_store)


# ---------------------------------------------------------------------------
# /alert mode
# ---------------------------------------------------------------------------

def _alert_mode(parts, chat_id, notifier, state):
    if len(parts) < 3:
        notifier.reply(chat_id, "Usage: <b>/alert mode claude|manual|both</b>")
        return
    mode = parts[2].lower()
    if mode not in ("claude", "manual", "both"):
        notifier.reply(chat_id, "Unknown mode. Use <b>claude</b>, <b>manual</b>, or <b>both</b>.")
        return
    state.alert_mode = mode
    state.save()
    label = _MODE_LABELS[mode]
    notifier.reply(chat_id, f"✅ Alert mode set to <b>{mode}</b> — firing {label}.")
    logger.info(f"Alert mode switched to {mode}.")


# ---------------------------------------------------------------------------
# /alert add (and shorthand)
# ---------------------------------------------------------------------------

def _alert_add(ticker, rest, chat_id, notifier, custom_store):
    if not rest:
        notifier.reply(chat_id, _help_text())
        return

    entries, errors = CustomAlertsStore.parse_entries(ticker, rest)

    if errors:
        notifier.reply(chat_id, "Some entries could not be parsed:\n" +
                       "\n".join(f"  ⚠️ {e}" for e in errors))

    if not entries:
        return

    custom_store.add(ticker, entries)
    logger.info(f"Added {len(entries)} custom alert(s) for {ticker}.")

    lines = [f"✅ <b>{ticker}</b> — {len(entries)} alert(s) added:"]
    for e in entries:
        signal = _SIGNAL.get(e.alert_type, {}).get(min(e.confidence, 5), "🔵")
        note_str = f" — {e.note}" if e.note else ""
        lines.append(f"  {signal} {e.price_str}  {e.alert_type}  (conf {e.confidence}){note_str}")
    notifier.reply(chat_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# /alert list
# ---------------------------------------------------------------------------

def _alert_list(parts, chat_id, notifier, custom_store):
    # Parse tokens after "list"
    tokens = [p.lower() for p in parts[2:]]

    filter_src = None  # None = both; "bot" = Claude only; "me" = manual only
    if "bot" in tokens:
        filter_src = "bot"
        tokens = [t for t in tokens if t != "bot"]
    elif "me" in tokens:
        filter_src = "me"
        tokens = [t for t in tokens if t != "me"]

    ticker = tokens[0].upper() if tokens else None

    if ticker:
        _list_ticker(ticker, filter_src, chat_id, notifier, custom_store)
    else:
        _list_all(filter_src, chat_id, notifier, custom_store)


def _list_ticker(ticker, filter_src, chat_id, notifier, custom_store):
    """Send alert listing for a single ticker (one message per source)."""
    if filter_src in (None, "bot"):
        notifier.reply(chat_id, _fmt_bot_alerts(ticker))
    if filter_src in (None, "me"):
        notifier.reply(chat_id, _fmt_manual_alerts(ticker, custom_store.list_alerts(ticker)))


def _list_all(filter_src, chat_id, notifier, custom_store):
    """Send one message per ticker across all known tickers."""
    from .parser import load_all_stocks
    bot_tickers = {cfg.ticker for cfg in load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)}
    manual_tickers = set(custom_store.all_tickers())
    all_tickers = sorted(bot_tickers | manual_tickers)

    if not all_tickers:
        notifier.reply(chat_id, "No alerts on record.")
        return

    for ticker in all_tickers:
        _list_ticker(ticker, filter_src, chat_id, notifier, custom_store)


def _fmt_bot_alerts(ticker: str) -> str:
    from .parser import parse_stock_file
    stock_file = STOCKS_DIR / f"{ticker}.md"
    if not stock_file.exists():
        return f"🤖 <b>{ticker}</b> — no stock file found."
    cfg = parse_stock_file(stock_file)
    levels = cfg.levels if cfg else []
    if not levels:
        return f"🤖 <b>{ticker}</b> — no Claude alerts found."
    lines = [f"🤖 <b>{ticker}</b> — {len(levels)} Claude alert(s):"]
    for lvl in levels:
        lines.append(f"  {lvl.signal} <b>{lvl.price_str}</b>  {lvl.alert_type}  {lvl.message}")
    return "\n".join(lines)


def _fmt_manual_alerts(ticker: str, entries) -> str:
    if not entries:
        return f"🐱 <b>{ticker}</b> — no manual alerts."
    lines = [f"🐱 <b>{ticker}</b> — {len(entries)} manual alert(s):"]
    for e in entries:
        signal = _SIGNAL.get(e.alert_type, {}).get(min(e.confidence, 5), "🔵")
        note_str = f" — {e.note}" if e.note else ""
        lines.append(f"  {signal} <b>{e.price_str}</b>  {e.alert_type}  (conf {e.confidence}){note_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /alert clear
# ---------------------------------------------------------------------------

def _alert_clear(parts, chat_id, notifier, custom_store):
    if len(parts) < 3:
        notifier.reply(
            chat_id,
            "Usage:\n"
            "  <code>/alert clear TICKER</code> — clear manual alerts for a stock\n"
            "  <code>/alert clear me</code>     — clear ALL manual alerts\n"
            "  <code>/alert clear undo</code>   — restore last cleared alerts"
        )
        return

    target = parts[2].lower()

    if target == "undo":
        result = custom_store.restore_backup()
        if result is None:
            notifier.reply(chat_id, "⚠️ No backup found to restore.")
        else:
            count, tickers = result
            notifier.reply(chat_id, f"↩️ Restored {count} alert(s) across {tickers} ticker(s).")
        return

    if target == "me":
        count, tickers = custom_store.clear_all()
        if count == 0:
            notifier.reply(chat_id, "Nothing to clear — no manual alerts on record.")
        else:
            notifier.reply(
                chat_id,
                f"🗑️ Cleared {count} manual alert(s) across {tickers} ticker(s).\n"
                f"Use <code>/alert clear undo</code> to restore."
            )
        logger.info(f"Cleared all manual alerts ({count} across {tickers} tickers).")
        return

    # /alert clear TICKER
    ticker = target.upper()
    count = custom_store.clear(ticker)
    if count:
        notifier.reply(
            chat_id,
            f"🗑️ <b>{ticker}</b> — {count} manual alert(s) cleared.\n"
            f"Use <code>/alert clear undo</code> to restore."
        )
        logger.info(f"Cleared {count} manual alerts for {ticker}.")
    else:
        notifier.reply(chat_id, f"<b>{ticker}</b> — no manual alerts to clear.")


# ---------------------------------------------------------------------------
# /backtest
# ---------------------------------------------------------------------------

def _handle_backtest(parts: list, chat_id: int, notifier: DiscordNotifier) -> None:
    if len(parts) < 2:
        notifier.reply(
            chat_id,
            "Usage: <code>/backtest TICKER</code>\n"
            "Shows floor/ceiling context for each alert level from pre-computed backtest data.\n"
            "Run <code>python3 backtest_levels.py TICKER --period 5y</code> first."
        )
        return

    ticker = parts[1].upper()

    from .floor_context import level_floor_summary
    results = level_floor_summary(ticker)

    if results is None:
        notifier.reply(
            chat_id,
            f"📭 No backtest data for <b>{ticker}</b>.\n"
            f"Run: <code>python3 backtest_levels.py {ticker} --period 5y</code>"
        )
        return

    buy_lines, sell_lines, watch_lines = [], [], []
    for r in results:
        sig   = r["signal"]
        ps    = r["price_str"]
        hint  = r["hint"]
        n     = r["n"]
        n_tag = f" ({n} entries)" if n > 0 else ""
        line  = f"{sig} {ps}  → {hint}{n_tag}"
        if r["alert_type"] == "BUY":
            buy_lines.append(line)
        elif r["alert_type"] == "SELL":
            sell_lines.append(line)
        else:
            watch_lines.append(line)

    sections = []
    if buy_lines:
        sections.append("<b>BUY zones:</b>\n" + "\n".join(buy_lines))
    if sell_lines:
        sections.append("<b>SELL zones:</b>\n" + "\n".join(sell_lines))
    if watch_lines:
        sections.append("<b>WATCH zones:</b>\n" + "\n".join(watch_lines))

    body = "\n\n".join(sections) if sections else "No levels found."
    msg = (
        f"📊 <b>{ticker}</b> — Floor Context\n\n"
        f"{body}\n\n"
        f"<i>Refresh: <code>python3 backtest_levels.py {ticker} --period 5y</code></i>"
    )
    notifier.reply(chat_id, msg)


# ---------------------------------------------------------------------------
# /analyze
# ---------------------------------------------------------------------------

def _analyze_clear(chat_id, notifier):
    pending = [f for f in REQUESTS_DIR.glob("*.json")]
    if not pending:
        notifier.reply(chat_id, "No queued analyses to clear.")
        return
    for f in pending:
        f.unlink()
    tickers = ", ".join(sorted({f.stem.split("_")[0] for f in pending}))
    notifier.reply(chat_id, f"🗑️ Cleared {len(pending)} queued analysis request(s): {tickers}")
    logger.info(f"Cleared {len(pending)} pending analysis request(s).")


def _analyze_list(chat_id, notifier):
    pending = sorted(REQUESTS_DIR.glob("*.json"))
    if not pending:
        notifier.reply(chat_id, "📭 No analyses queued.")
        return
    lines = [f"📋 <b>{len(pending)} analysis request(s) queued:</b>\n"]
    for f in pending:
        data = json.loads(f.read_text(encoding="utf-8"))
        ticker = data.get("ticker", f.stem.split("_")[0])
        mode = data.get("mode", "comprehensive")
        retro_str = " + retro" if data.get("retro") else ""
        requested_at = data.get("requested_at", "")[:16]
        lines.append(f"  • <b>{ticker}</b> — {mode}{retro_str}  ({requested_at})")
    lines.append(
        "\nUse <code>/analyze delete TICKER</code> to remove one, "
        "or <code>/analyze list clear</code> to clear all."
    )
    notifier.reply(chat_id, "\n".join(lines))


def _analyze_delete(ticker, chat_id, notifier):
    matching = sorted(REQUESTS_DIR.glob(f"{ticker}_*.json"))
    if not matching:
        notifier.reply(chat_id, f"📭 No queued requests found for <b>{ticker}</b>.")
        return
    for f in matching:
        f.unlink()
    count = len(matching)
    notifier.reply(chat_id, f"🗑️ Removed {count} request(s) for <b>{ticker}</b> from the queue.")
    logger.info(f"Deleted {count} queued request(s) for {ticker}.")


def _handle_analyze(parts, chat_id, notifier):
    if len(parts) < 2:
        notifier.reply(
            chat_id,
            "Usage: <b>/analyze TICKER [mode] [--update|--no-update]</b>\n"
            "Modes: <code>comprehensive</code> (default), <code>chart-news</code>, "
            "<code>chart-news-community</code>, <code>chart-only</code>\n"
            "e.g. <code>/analyze SUVEN chart-news --no-update</code>"
        )
        return

    if parts[1].lower() == "clear":
        _analyze_clear(chat_id, notifier)
        return

    if parts[1].lower() == "list":
        if len(parts) >= 3 and parts[2].lower() == "clear":
            _analyze_clear(chat_id, notifier)
        else:
            _analyze_list(chat_id, notifier)
        return

    if parts[1].lower() == "delete":
        if len(parts) < 3:
            notifier.reply(chat_id, "Usage: <code>/analyze delete TICKER</code>")
            return
        _analyze_delete(parts[2].upper(), chat_id, notifier)
        return

    # Tolerate mode-first usage: /analyze chart-news-community SUVEN
    # (canonical order is /analyze TICKER [mode], but users frequently type the
    # mode first and that previously got jammed into the ticker field).
    if parts[1].lower() in _VALID_MODES:
        if len(parts) < 3:
            notifier.reply(
                chat_id,
                "Usage: <b>/analyze TICKER [mode]</b> — ticker is required.\n"
                f"Looks like you started with the mode <code>{parts[1].lower()}</code>. "
                f"Try <code>/analyze SUVEN {parts[1].lower()}</code>."
            )
            return
        ticker = parts[2].upper()
        remaining = [parts[1].lower()] + [p.lower() for p in parts[3:]]
    else:
        ticker = parts[1].upper()
        remaining = [p.lower() for p in parts[2:]]

    update = True
    if "--no-update" in remaining:
        update = False
        remaining.remove("--no-update")
    elif "--update" in remaining:
        remaining.remove("--update")

    chat_mode = False
    if "--chat" in remaining:
        chat_mode = True
        remaining.remove("--chat")

    retro = False
    if "-retro" in remaining:
        retro = True
        remaining.remove("-retro")

    backtest = False
    if "-backtest" in remaining:
        backtest = True
        remaining.remove("-backtest")

    react = False
    if "--react" in remaining:
        react = True
        remaining.remove("--react")
    elif "--no-react" in remaining:
        remaining.remove("--no-react")

    forecast = False
    if "-forecast" in remaining:
        forecast = True
        remaining.remove("-forecast")

    # Parse period (used by retrospective mode, -retro, and -backtest flags)
    retro_period = _DEFAULT_PERIOD
    backtest_period = "5y"
    for token in remaining:
        if token in _VALID_PERIODS:
            retro_period = token
            backtest_period = token
            break

    mode = "comprehensive"
    for token in remaining:
        if token in _VALID_MODES:
            mode = token
            break

    # ── retrospective mode — its own fixed flow ──────────────────────────────
    if mode == "retrospective":
        if retro:
            notifier.reply(chat_id, "❌ <code>-retro</code> cannot be combined with <code>retrospective</code> mode.")
            return
        IST = pytz.timezone(MARKET_TIMEZONE)
        now = datetime.now(IST)
        REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        request_file = REQUESTS_DIR / f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}.json"
        request_file.write_text(json.dumps({
            "ticker": ticker,
            "mode": "retrospective",
            "period": retro_period,
            "update": False,
            "chat": False,
            "react": react,
            "requested_at": now.isoformat(),
            "chat_id": chat_id,
        }, indent=2), encoding="utf-8")
        logger.info(f"Retrospective request queued: {request_file.name} (period={retro_period})")
        notifier.reply(
            chat_id,
            f"🔬 <b>{ticker}</b> retrospective analysis queued.\n"
            f"Period: <b>{retro_period}</b>\n"
            f"Claude will run the floor signal analysis, update <b>## Floor Signals</b> "
            f"in stocks/{ticker}.md, and send you a summary."
        )
        return

    # ── --chat not available in comprehensive or full (PDF-only modes) ───────
    if chat_mode and mode in ("comprehensive", "full"):
        notifier.reply(
            chat_id,
            f"❌ <b>--chat</b> is not available in <b>{mode}</b> mode.\n"
            f"{mode.capitalize()} generates full fundamentals, belief analysis and thesis "
            f"changes that need the PDF format to present properly.\n"
            "Use <code>chart-news</code>, <code>chart-news-community</code>, or "
            "<code>chart-only</code> with <b>--chat</b>."
        )
        return

    IST = pytz.timezone(MARKET_TIMEZONE)
    now = datetime.now(IST)

    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    request_file = REQUESTS_DIR / f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}.json"
    request_file.write_text(json.dumps({
        "ticker": ticker,
        "mode": mode,
        "retro": retro or mode == "full",   # full always implies retro
        "retro_period": retro_period,
        "backtest": backtest or mode == "full",  # full implies backtest
        "backtest_period": backtest_period,
        "forecast": forecast or mode == "full",  # full implies forecast
        "update": update,
        "chat": chat_mode,
        "react": react or mode == "full",      # full implies react
        "requested_at": now.isoformat(),
        "chat_id": chat_id,
    }, indent=2), encoding="utf-8")

    is_full = mode == "full"
    logger.info(f"Analysis request queued: {request_file.name} "
                f"(mode={mode}, retro={retro or is_full}, backtest={backtest or is_full}, "
                f"forecast={forecast or is_full}, "
                f"react={react or is_full}, update={update}, chat={chat_mode})")
    retro_note    = (f"\nRetrospective calibration: <b>on</b> (period: {retro_period})." if (retro or is_full) else "")
    backtest_note = (f"\nBacktest refresh: <b>on</b> (period: {backtest_period})." if (backtest or is_full) else "")
    forecast_note = ("\nPrice forecast: <b>on</b> (Prophet 30/60/90 day)." if (forecast or is_full) else "")
    react_note    = "\nReaction feedback: <b>included</b> (--react)." if (react or is_full) else ""
    update_note = (
        "Stock file will be <b>updated</b> (relevant sections only)."
        if update else
        f"Draft saved to <b>nightly-levels/{ticker}.md</b> — stock file untouched."
    )
    output_note = "Results sent as <b>Telegram messages</b>." if chat_mode else "A <b>PDF report</b> will be sent."
    notifier.reply(
        chat_id,
        f"🔍 <b>{ticker}</b> analysis queued.\n"
        f"Mode: <b>{mode}</b> · {update_note}{retro_note}{backtest_note}{forecast_note}{react_note}\n"
        f"{output_note}\n"
        f"Claude will pick this up on the next /loop cycle."
    )


# ---------------------------------------------------------------------------
# /forecast
# ---------------------------------------------------------------------------

def _handle_forecast(parts: list, chat_id: int, notifier: DiscordNotifier) -> None:
    """
    /forecast TICKER          — instant read-only forecast summary
    /forecast TICKER -update  — forecast-driven alert level adjustments (queued)
    """
    if len(parts) < 2:
        notifier.reply(
            chat_id,
            "<b>/forecast — price forecast</b>\n\n"
            "<code>/forecast TICKER</code> — instant 30/60/90 day forecast\n"
            "<code>/forecast TICKER -update</code> — adjust alert levels based on forecast"
        )
        return

    ticker = parts[1].upper()
    remaining = [p.lower() for p in parts[2:]]

    do_update = "-update" in remaining

    if do_update:
        # Queue a forecast-update request for the Claude loop to pick up
        IST = pytz.timezone(MARKET_TIMEZONE)
        now = datetime.now(IST)
        REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        request_file = REQUESTS_DIR / f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}.json"
        request_file.write_text(json.dumps({
            "ticker": ticker,
            "mode": "forecast-update",
            "forecast": True,
            "update": True,
            "chat": False,
            "requested_at": now.isoformat(),
            "chat_id": chat_id,
        }, indent=2), encoding="utf-8")
        notifier.reply(
            chat_id,
            f"📊 <b>{ticker}</b> forecast-update queued.\n"
            f"Claude will run the forecast, adjust alert levels in stocks/{ticker}.md, "
            f"and send a summary."
        )
        return

    # Instant forecast — run Prophet + ExponentialSmoothing right here
    notifier.reply(chat_id, f"📊 Running forecast for <b>{ticker}</b>…")

    try:
        from .floor_context import _load_ohlc
        from .forecaster import prophet_forecast, trend_forecast

        df = _load_ohlc(ticker)
        if df is None or len(df) < 30:
            notifier.reply(chat_id, f"❌ Not enough OHLC data for {ticker}. Run an analysis first.")
            return

        closes = df["Close"]
        lines = [f"📊 <b>{ticker} Forecast</b>\n"]

        # Short-term trend (ExponentialSmoothing)
        tf = trend_forecast(closes, horizon=10)
        if tf and tf.confidence > 0.3:
            arrow = {"up": "↑", "down": "↓", "flat": "→"}.get(tf.trend_direction, "→")
            lines.append(
                f"<b>Trend:</b> {arrow} {tf.trend_direction} "
                f"(strength: ₹{abs(tf.trend_strength):,.1f}/day, "
                f"confidence: {tf.confidence:.0%})"
            )
            lines.append(
                f"<b>10-day range:</b> ₹{tf.lower[-1]:,.0f} – ₹{tf.upper[-1]:,.0f}"
            )
        else:
            lines.append("<b>Trend:</b> insufficient data for short-term forecast")

        lines.append("")

        # Long-term forecast (Prophet)
        pf = prophet_forecast(closes)
        if pf:
            for h in sorted(pf.keys()):
                vals = pf[h]
                arrow = {"up": "↑", "down": "↓", "sideways": "→"}.get(vals["trend"], "→")
                lines.append(
                    f"<b>{h}-day:</b> ₹{vals['lower']:,.0f} – "
                    f"₹{vals['upper']:,.0f} "
                    f"(predicted ₹{vals['predicted']:,.0f}) {arrow}"
                )
        elif len(closes) < 120:
            lines.append("Long-term forecast unavailable (need 6+ months of data).")
        else:
            lines.append(
                "Long-term forecast: too volatile for reliable prediction. "
                "Using ATR-based estimates only."
            )

        # Cross-reference with alert zones
        from .parser import load_all_stocks
        all_stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
        stock = next((s for s in all_stocks if s.ticker == ticker), None)
        if stock and pf:
            lines.append("\n<b>Alert zones vs forecast:</b>")
            fc_30 = pf.get(30, {})
            fc_90 = pf.get(90, {})
            for level in stock.levels:
                mid = (level.lower + level.upper) / 2
                note = ""
                if level.alert_type == "BUY" and fc_30 and fc_30.get("lower", 999999) <= level.upper:
                    note = " — <i>within 30-day range, may be tested</i>"
                elif level.alert_type == "BUY" and fc_90 and fc_90.get("lower", 999999) <= level.upper:
                    note = " — <i>within 90-day range</i>"
                elif level.alert_type == "SELL" and fc_30 and fc_30.get("upper", 0) >= level.lower:
                    note = " — <i>within 30-day range, may be reached</i>"
                elif level.alert_type == "SELL" and fc_90 and fc_90.get("upper", 0) >= level.lower:
                    note = " — <i>within 90-day range</i>"
                else:
                    note = " — <i>outside forecast range</i>"
                lines.append(
                    f"  {level.signal} {level.alert_type} {level.price_str}{note}"
                )

        notifier.reply(chat_id, "\n".join(lines))

    except Exception as e:
        logger.error(f"Forecast failed for {ticker}: {e}", exc_info=True)
        notifier.reply(chat_id, f"❌ Forecast failed: {e}")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

_HELP_OVERVIEW = (
    "<b>TradeCentral — Commands</b>\n\n"
    "<code>/alert</code>    — add, list, or clear price alerts\n"
    "<code>/analyze</code>  — queue a stock analysis (PDF or chat)\n"
    "<code>/backtest</code> — show floor/ceiling context for each alert level\n"
    "<code>/forecast</code> — price forecast (30/60/90 day)\n"
    "<code>/note</code>     — share a thought about a stock\n"
    "<code>/react</code>    — reaction emoji legend and history\n"
    "<code>/mmi</code>      — Market Mood Index pin mode\n"
    "<code>/portfolio</code> — list, add, or remove tickers in the portfolio\n"
    "<code>/help</code>     — this message\n\n"
    "Use <code>/help alert</code>, <code>/help analyze</code>, <code>/help note</code>, <code>/help react</code>, "
    "<code>/help mmi</code>, or <code>/help portfolio</code> for full usage of each command."
)

_HELP_DETAIL: dict[str, str] = {
    "analyze": (
        "<b>/analyze — queue a stock analysis</b>\n\n"
        "<code>/analyze TICKER [mode] [flags]</code>\n\n"
        "<b>Modes:</b>\n"
        "  <code>comprehensive</code>       — full analysis, PDF (default)\n"
        "  <code>full</code>                — everything (retro + backtest + react + comprehensive), PDF\n"
        "  <code>chart-news-community</code> — price + news + sentiment, PDF\n"
        "  <code>chart-news</code>           — price + news, PDF\n"
        "  <code>chart-only</code>           — price and levels only, PDF\n"
        "  <code>retrospective</code>        — floor signal calibration only\n\n"
        "<b>Flags:</b>\n"
        "  <code>--update</code>     update stocks/TICKER.md (default)\n"
        "  <code>--no-update</code>  save draft to nightly-levels/ instead\n"
        "  <code>--chat</code>       send results as Telegram messages, not PDF\n"
        "                   (not available with comprehensive or full)\n"
        "  <code>-retro</code>       run floor calibration before analysis\n"
        "  <code>-backtest</code>    refresh backtest data before analysis (default period 5y)\n"
        "  <code>-forecast</code>    include Prophet price forecast in analysis\n"
        "  <code>1y 2y 3y 5y max</code>  period for -retro / -backtest\n"
        "  <code>--react</code>      include your emoji reactions in floor analysis\n"
        "  <code>--no-react</code>   exclude reactions (default)\n\n"
        "<b>Queue management:</b>\n"
        "  <code>/analyze list</code>          — show all queued requests\n"
        "  <code>/analyze delete TICKER</code> — remove one ticker from queue\n"
        "  <code>/analyze clear</code>         — delete all pending requests\n"
        "  <code>/analyze list clear</code>    — same as clear (alias)\n\n"
        "<b>Examples:</b>\n"
        "  <code>/analyze SUVEN</code>\n"
        "  <code>/analyze BBOX full 5y</code>\n"
        "  <code>/analyze BBOX -backtest</code>\n"
        "  <code>/analyze BBOX -retro -backtest</code>\n"
        "  <code>/analyze CGPOWER chart-only --chat</code>\n"
        "  <code>/analyze STLTECH chart-news --no-update</code>\n\n"
        "<b>/backtest TICKER</b> — instant floor/ceiling context from pre-computed data\n"
        "  Run <code>python3 backtest_levels.py TICKER --period 5y</code> first"
    ),
    "mmi": (
        "<b>/mmi — Market Mood Index pin mode</b>\n\n"
        "  <code>/mmi compact</code>  silently update pinned message on each poll (default)\n"
        "  <code>/mmi full</code>     send and pin a full message on each zone change"
    ),
    "note": (
        "<b>/note — share a thought about a stock</b>\n\n"
        "  <code>/note TICKER your message</code>\n\n"
        "e.g. <code>/note STLTECH I think ₹500 support is weak</code>\n\n"
        "Same as replying to a bot alert — your thought is logged and\n"
        "queued for the next analysis run. Use this when you don't have\n"
        "a recent alert to reply to."
    ),
    "react": (
        "<b>/react — Reaction feedback</b>\n\n"
        "  <code>/react legend</code>  show accepted emojis and what they mean\n"
        "  <code>/react list</code>    show your reactions from the past 7 days\n\n"
        "React to any bot alert with an emoji to log feedback.\n"
        "This feedback is used in future analyses when <code>--react</code> is passed."
    ),
    "portfolio": (
        "<b>/portfolio — manage portfolio tickers</b>\n\n"
        "  <code>/portfolio list</code>                — show active stocks\n"
        "  <code>/portfolio add TICKER</code>          — add a ticker; queues a full analysis\n"
        "  <code>/portfolio add TICKER --no-analyze</code>  — add stub only, skip analysis\n"
        "  <code>/portfolio archive TICKER</code>      — archive (full sweep, Yes/No confirm)\n"
        "  <code>/portfolio restore TICKER</code>      — bring an archived ticker back\n"
        "  <code>/portfolio archived</code>            — list archived stocks\n\n"
        "Add fetches yfinance metadata, writes <code>stocks/TICKER.md</code> from the\n"
        "template, and (by default) queues a full chart-news-community-retro-\n"
        "backtest-forecast analysis. Archive sweeps the watchlist file, KB dossier,\n"
        "analysis sidecars and reports into <code>archive/TICKER/</code> and stops the\n"
        "bot watching it — fully recoverable via restore. (<code>remove</code> is an\n"
        "alias of archive.) All auto-sync the Active stocks table in README.md."
    ),
}


# ---------------------------------------------------------------------------
# /note — send a thought about a stock (same as reply-to-alert, without needing one)
# ---------------------------------------------------------------------------

def _handle_note(
    parts: list, full_text: str, chat_id: int,
    notifier: DiscordNotifier, convo_store: "ConversationStore | None",
) -> None:
    if len(parts) < 3:
        notifier.reply(
            chat_id,
            "<b>/note — share a thought about a stock</b>\n\n"
            "  <code>/note TICKER your message here</code>\n\n"
            "e.g. <code>/note STLTECH I think ₹500 support is weak</code>\n\n"
            "Same as replying to an alert — logs your thought and queues\n"
            "it for the next analysis run.",
        )
        return

    ticker = parts[1].upper()
    # Extract the note text: everything after "/note TICKER "
    offset = len(parts[0]) + 1 + len(parts[1]) + 1
    note_text = full_text[offset:].strip()

    if not note_text:
        notifier.reply(chat_id, "Note is empty. Usage: <code>/note TICKER your message</code>")
        return

    now = datetime.now(pytz.timezone(MARKET_TIMEZONE))

    # Log to conversations.jsonl (same store as reply-to-alert)
    if convo_store:
        convo_store.log_reply(
            user_message=note_text,
            original_message_id=0,      # no originating bot message
            alert_context=None,
            ticker=ticker,
            replied_at=now,
        )

    # Create a chat request in requests/ (same as reply-to-alert)
    request = {
        "ticker": ticker,
        "mode": "chat",
        "chat": True,
        "user_message": note_text,
        "original_bot_message": "",     # no originating bot message
        "alert_context": None,
        "chat_id": chat_id,
        "reply_to_message_id": None,
        "source": "note",               # distinguish from reply-to-alert
        "requested_at": now.isoformat(),
    }
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{ticker}_chat_{now.strftime('%Y%m%d_%H%M%S')}.json"
    request_path = REQUESTS_DIR / filename
    try:
        request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
        logger.info(f"Note request created: {filename}")
        # Persist to portfolio_notes.jsonl for KB synthesis by the autonomous loop.
        # Regular replies stay in conversations.jsonl only; /note is the explicit
        # signal that this observation should outlive the chat session.
        notes_path = Path("data/portfolio_notes.jsonl")
        note_record = {
            "id": f"{ticker}::{now.strftime('%Y%m%d_%H%M%S')}",
            "ticker": ticker,
            "note": note_text,
            "timestamp": now.isoformat(),
            "processed": False,
        }
        with notes_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(note_record) + "\n")
        notifier.reply(chat_id, f"💭 Got it — thinking about <b>{ticker}</b>...")
    except OSError as e:
        logger.error(f"Could not write note request: {e}")
        notifier.reply(chat_id, "⚠️ Could not save note.")


# ---------------------------------------------------------------------------
# /react — reaction legend and recent history
# ---------------------------------------------------------------------------

# Human-friendly descriptions for each reaction emoji
_EMOJI_DESCRIPTIONS: dict[str, str] = {
    "👍": "I took this trade / acted on this alert",
    "👎": "I disagree with this call",
    "⏳": "Good alert — watching, waiting for better entry",
    "✅": "This trade was profitable in hindsight",
    "❌": "This trade was not profitable",
    "🎯": "Perfect timing and level",
}


def _handle_react(
    parts: list, chat_id: int, notifier: DiscordNotifier,
    feedback_store: FeedbackStore | None,
) -> None:
    if len(parts) < 2:
        notifier.reply(chat_id, "Use <code>/react legend</code> or <code>/react list</code>.")
        return

    verb = parts[1].lower()

    if verb == "legend":
        _react_legend(chat_id, notifier)
    elif verb == "list":
        _react_list(chat_id, notifier, feedback_store)
    else:
        notifier.reply(chat_id, "Unknown subcommand. Use <code>/react legend</code> or <code>/react list</code>.")


def _react_legend(chat_id: int, notifier: DiscordNotifier) -> None:
    """Send the emoji legend showing accepted reactions and their meanings."""
    lines = ["<b>Reaction emoji legend:</b>\n"]
    for emoji, desc in _EMOJI_DESCRIPTIONS.items():
        meaning = EMOJI_MEANINGS[emoji]
        lines.append(f"  {emoji}  <code>{meaning}</code> — {desc}")
    lines.append("\n<i>No reaction = no action taken (default)</i>")
    lines.append("<i>React to any bot alert to log feedback.</i>")
    notifier.reply(chat_id, "\n".join(lines))


def _react_list(
    chat_id: int, notifier: DiscordNotifier,
    feedback_store: FeedbackStore | None,
) -> None:
    """Show the last 7 days of reactions."""
    if feedback_store is None:
        notifier.reply(chat_id, "Feedback store not available.")
        return

    all_records = feedback_store.load_all()
    if not all_records:
        notifier.reply(chat_id, "No reactions logged yet.")
        return

    # Filter to last 7 days
    now = datetime.now()
    week_ago = now.timestamp() - 7 * 86400
    recent = []
    for r in all_records:
        try:
            reacted = datetime.fromisoformat(r["reacted_at"])
            if reacted.timestamp() >= week_ago:
                recent.append(r)
        except (KeyError, ValueError):
            continue

    if not recent:
        notifier.reply(chat_id, "No reactions in the last 7 days.")
        return

    # Format: group by day, show emoji + ticker + alert type + price
    lines = [f"<b>Reactions — last 7 days ({len(recent)} total):</b>\n"]
    for r in recent[-20:]:  # cap at 20 most recent to avoid message length limits
        emoji = r.get("emoji", "?")
        ticker = r.get("ticker", "?")
        alert_type = r.get("alert_type", "")
        price_str = r.get("price_str", "")
        reacted = r.get("reacted_at", "")[:10]  # date only
        lines.append(f"  {reacted}  {emoji}  <b>{ticker}</b> {alert_type} {price_str}")

    if len(recent) > 20:
        lines.append(f"\n<i>... and {len(recent) - 20} more</i>")
    notifier.reply(chat_id, "\n".join(lines))


def _handle_help(parts: list, chat_id: int, notifier: DiscordNotifier) -> None:
    if len(parts) < 2:
        notifier.reply(chat_id, _HELP_OVERVIEW)
        return

    topic = parts[1].lower().lstrip("/")

    if topic == "alert":
        notifier.reply(chat_id, _help_text())
    elif topic in _HELP_DETAIL:
        notifier.reply(chat_id, _HELP_DETAIL[topic])
    else:
        notifier.reply(
            chat_id,
            f"No help found for <code>{topic}</code>.\n"
            "Available: <code>alert</code>, <code>analyze</code>, <code>note</code>, <code>react</code>, <code>mmi</code>, <code>portfolio</code>"
        )


# ---------------------------------------------------------------------------
# /portfolio — list / add / remove tickers in the portfolio
# ---------------------------------------------------------------------------

def _handle_portfolio(parts: list, chat_id: int, notifier: DiscordNotifier) -> None:
    """Dispatcher for /portfolio list|add|archive|restore|archived|remove."""
    if len(parts) < 2:
        notifier.reply(chat_id, _HELP_DETAIL["portfolio"])
        return

    verb = parts[1].lower()

    if verb == "list":
        _portfolio_list(chat_id, notifier)
    elif verb == "add":
        if len(parts) < 3:
            notifier.reply(chat_id, "Usage: <code>/portfolio add TICKER [--no-analyze]</code>")
            return
        ticker = parts[2].upper()
        queue_analysis = "--no-analyze" not in parts
        _portfolio_add(ticker, queue_analysis, chat_id, notifier)
    elif verb in ("archive", "remove"):
        # "remove" kept as an alias for muscle memory; both archive with a full sweep.
        if len(parts) < 3:
            notifier.reply(chat_id, "Usage: <code>/portfolio archive TICKER</code>")
            return
        ticker = parts[2].upper()
        _portfolio_archive_prompt(ticker, chat_id, notifier)
    elif verb == "restore":
        if len(parts) < 3:
            notifier.reply(chat_id, "Usage: <code>/portfolio restore TICKER</code>")
            return
        ticker = parts[2].upper()
        _portfolio_restore(ticker, chat_id, notifier)
    elif verb == "archived":
        _portfolio_archived_list(chat_id, notifier)
    else:
        notifier.reply(
            chat_id,
            f"Unknown verb <code>{verb}</code>. Try "
            "<code>/portfolio list</code>, <code>/portfolio add TICKER</code>, "
            "<code>/portfolio archive TICKER</code>, <code>/portfolio restore TICKER</code>, "
            "or <code>/portfolio archived</code>.",
        )


def _portfolio_list(chat_id: int, notifier: DiscordNotifier) -> None:
    """Render the portfolio as an HTML list message."""
    from . import portfolio
    from pathlib import Path

    rows: list[tuple[str, str, int]] = []
    stocks_dir = Path(portfolio.STOCKS_DIR)
    for md in sorted(stocks_dir.glob("*.md")):
        if md.name == "_TEMPLATE.md":
            continue
        sector, core_pct = portfolio._read_identity(md)
        if sector is None:
            continue
        rows.append((md.stem.upper(), sector, core_pct))

    if not rows:
        notifier.reply(chat_id, "Portfolio is empty.")
        return

    lines = [f"<b>📒 Portfolio — {len(rows)} stocks</b>\n"]
    for ticker, sector, core_pct in rows:
        lines.append(f"<code>{ticker:<12}</code> {core_pct:>2}%  <i>{sector}</i>")
    notifier.reply(chat_id, "\n".join(lines))


def _portfolio_add(
    ticker: str, queue_analysis: bool, chat_id: int, notifier: DiscordNotifier
) -> None:
    """Add a ticker. Sends progress acknowledgement, then result."""
    from . import portfolio

    notifier.reply(chat_id, f"⏳ Fetching metadata for <b>{ticker}</b>...")

    try:
        result = portfolio.add_ticker(ticker, queue_analysis=queue_analysis)
    except Exception as exc:
        logger.exception("portfolio.add_ticker failed for %s", ticker)
        notifier.reply(chat_id, f"❌ Failed to add {ticker}: {exc}")
        return

    if not result.get("ok"):
        notifier.reply(chat_id, f"❌ {result.get('error', 'Unknown error')}")
        return

    meta = result.get("metadata", {}) or {}
    sector = meta.get("sector") or "<i>unknown</i>"
    name = meta.get("long_name") or ticker
    price = meta.get("current_price")
    price_str = f"₹{price:.2f}" if price else "<i>unknown</i>"

    parts_msg = [
        f"✅ Added <b>{ticker}</b> to portfolio",
        f"   {name}",
        f"   Sector: {sector}",
        f"   Price: {price_str}",
        f"   File: <code>{result['stock_file']}</code>",
    ]
    if result.get("analysis_queued"):
        parts_msg.append("📨 Full analysis queued — report in next loop cycle.")
    elif queue_analysis:
        parts_msg.append("⚠️ Analysis queue failed — run <code>/analyze " + ticker + "</code> manually.")
    else:
        parts_msg.append("ℹ️ No analysis queued (--no-analyze).")
    if result.get("claude_md_synced"):
        parts_msg.append(f"📝 CLAUDE.md synced ({result.get('claude_md_count')} stocks).")

    notifier.reply(chat_id, "\n".join(parts_msg))


def _portfolio_archive_prompt(
    ticker: str, chat_id: int, notifier: DiscordNotifier
) -> None:
    """Post a ✅/❌ reaction confirm for the archive. The real action runs when the
    user reacts ✅ (handled by discord_client._handle_archive_reaction)."""
    from . import portfolio
    from .discord_client import register_pending_archive

    if not portfolio.is_in_portfolio(ticker):
        notifier.reply(chat_id, f"❌ {ticker} is not in the portfolio.")
        return

    mid = notifier.reply(
        chat_id,
        f"🗄️ Archive <b>{ticker}</b>?  React ✅ to confirm or ❌ to cancel.\n"
        f"Its watchlist file, KB dossier, analysis sidecars and reports move into "
        f"<code>archive/{ticker}/</code> and the bot stops watching it. "
        f"Fully recoverable with <code>/portfolio restore {ticker}</code>.",
    )
    if mid:
        register_pending_archive(mid, ticker, chat_id)


def _portfolio_restore(ticker: str, chat_id: int, notifier: DiscordNotifier) -> None:
    """Restore an archived ticker and re-queue analysis."""
    from . import portfolio

    try:
        result = portfolio.restore_ticker(ticker)
    except Exception as exc:
        logger.exception("portfolio.restore_ticker failed for %s", ticker)
        notifier.reply(chat_id, f"❌ Failed to restore {ticker}: {exc}")
        return

    if not result.get("ok"):
        notifier.reply(chat_id, f"❌ {result.get('error', 'Unknown error')}")
        return

    lines = [
        f"♻️ Restored <b>{ticker}</b> — {len(result.get('restored', []))} file(s) back in place.",
    ]
    if result.get("analysis_queued"):
        lines.append("📨 Fresh analysis queued — dossier will rebuild next loop cycle.")
    if result.get("table_synced"):
        lines.append(f"📝 README synced ({result.get('table_count')} stocks).")
    notifier.reply(chat_id, "\n".join(lines))


def _portfolio_archived_list(chat_id: int, notifier: DiscordNotifier) -> None:
    """Render the archive registry as an HTML list."""
    from . import portfolio

    archived = portfolio.list_archived()
    if not archived:
        notifier.reply(chat_id, "🗄️ No archived stocks.")
        return

    lines = [f"<b>🗄️ Archived — {len(archived)} stocks</b>\n"]
    for ticker in sorted(archived):
        meta = archived[ticker] or {}
        when = (meta.get("archived_at") or "")[:10]
        reason = meta.get("reason") or "—"
        lines.append(f"<code>{ticker:<12}</code> {when}  <i>{reason}</i>")
    lines.append("\nRestore one with <code>/portfolio restore TICKER</code>.")
    notifier.reply(chat_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# Archive confirmation — invoked by the Discord confirm button
# ---------------------------------------------------------------------------

def perform_archive(ticker: str) -> str:
    """Run the archive sweep and return the HTML result message.

    Pure: does the work and returns the text to display. Called when the user reacts
    ✅ on the archive prompt (alert_bot/discord_client._handle_archive_reaction).
    """
    from . import portfolio

    try:
        result = portfolio.archive_ticker(ticker, reason="archived via /portfolio")
    except Exception as exc:
        logger.exception("portfolio.archive_ticker failed for %s", ticker)
        return f"❌ Failed to archive {ticker}: {exc}"

    if not result.get("ok"):
        return f"❌ {result.get('error', 'Unknown error')}"

    text = (
        f"🗄️ Archived <b>{ticker}</b> — {result.get('artifacts_moved')} file(s) → "
        f"<code>archive/{ticker}/</code>"
    )
    if result.get("table_synced"):
        text += f"\n📝 README synced ({result.get('table_count')} stocks)."
    text += f"\n♻️ Restore anytime: <code>/portfolio restore {ticker}</code>"
    return text


def archive_cancel_text(ticker: str) -> str:
    return f"Cancelled — <b>{ticker}</b> remains in the portfolio."


# ---------------------------------------------------------------------------
# Help text (also used inline when /alert is called with bad args)
# ---------------------------------------------------------------------------

def _help_text() -> str:
    return (
        "<b>/alert commands:</b>\n\n"
        "<b>Add alerts:</b>\n"
        "  <code>/alert add TICKER price TYPE [conf] [note], ...</code>\n"
        "  <code>/alert TICKER price TYPE [conf] [note], ...</code>  (shorthand)\n"
        "  e.g. <code>/alert SUVEN 145 BUY 3 Earnings play, 135 BUY</code>\n\n"
        "<b>List alerts:</b>\n"
        "  <code>/alert list</code>              — all alerts, all tickers\n"
        "  <code>/alert list bot</code>          — Claude alerts, all tickers\n"
        "  <code>/alert list me</code>           — your alerts, all tickers\n"
        "  <code>/alert list TICKER</code>       — all alerts for a stock\n"
        "  <code>/alert list TICKER bot</code>   — Claude alerts for a stock\n"
        "  <code>/alert list TICKER me</code>    — your alerts for a stock\n\n"
        "<b>Clear alerts:</b>\n"
        "  <code>/alert clear TICKER</code>      — clear your alerts for a stock\n"
        "  <code>/alert clear me</code>          — clear ALL your alerts\n"
        "  <code>/alert clear undo</code>        — restore last clear\n\n"
        "<b>Mode:</b>\n"
        "  <code>/alert mode claude|manual|both</code>\n\n"
        "Conf 1–5 (default 3). Note is everything after the confidence number."
    )


# ---------------------------------------------------------------------------
# Command dispatch is driven by the Discord gateway (alert_bot/discord_client.py),
# which calls _handle / _handle_reply / log_reaction directly. The old Telegram
# long-poll loop (start/_listen_loop) and its callback_query plumbing are gone.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reply-to-message handler — user replies to a bot alert
# ---------------------------------------------------------------------------

# Regex to extract ticker from bot message text (e.g. "SUZLON at ₹40" or "📡 APPROACH SUZLON")
import re
_TICKER_RE = re.compile(r"\b([A-Z]{2,20})\b(?:\s+at\s+₹|\s+₹)")
_APPROACH_RE = re.compile(r"(?:APPROACH|FORECAST|REMINDER|BUY|SELL|WATCH)\s+([A-Z]{2,20})\b")


def _extract_ticker_from_text(text: str) -> "str | None":
    """Try to extract a stock ticker from a bot message's text."""
    # Try structured patterns first
    m = _APPROACH_RE.search(text)
    if m:
        return m.group(1)
    m = _TICKER_RE.search(text)
    if m:
        return m.group(1)
    return None


def _handle_reply(
    user_text: str,
    chat_id: int,
    user_message_id: int,
    reply_to: dict,
    notifier: DiscordNotifier,
    sent_registry: SentAlertsRegistry,
    convo_store: "ConversationStore",
) -> None:
    """
    Handle a user's text reply to a bot message.

    1. Look up the original message in sent_alerts registry for full context.
    2. If not found, try to extract the ticker from the original message text.
    3. Save to conversations.jsonl.
    4. Create a chat request in requests/ for the /loop to pick up.
    5. Acknowledge to the user.
    """
    original_msg_id = reply_to.get("message_id")
    original_text = reply_to.get("text", "")
    now = datetime.now(pytz.timezone(MARKET_TIMEZONE))

    # Try to get full alert context from sent_alerts registry
    alert_context = sent_registry.lookup(original_msg_id) if original_msg_id else None
    ticker = None

    if alert_context:
        ticker = alert_context.get("ticker")
    else:
        # Fall back to extracting ticker from the original message text
        ticker = _extract_ticker_from_text(original_text)

    # Save the conversation
    convo_store.log_reply(
        user_message=user_text,
        original_message_id=original_msg_id or 0,
        alert_context=alert_context,
        ticker=ticker,
        replied_at=now,
    )

    # Create a chat request for the /loop to process
    if ticker:
        request = {
            "ticker": ticker,
            "mode": "chat",
            "chat": True,
            "user_message": user_text,
            "original_bot_message": original_text[:500],  # truncate for safety
            "alert_context": alert_context,
            "chat_id": chat_id,
            "reply_to_message_id": user_message_id,
            "requested_at": now.isoformat(),
        }
        REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{ticker}_chat_{now.strftime('%Y%m%d_%H%M%S')}.json"
        request_path = REQUESTS_DIR / filename
        try:
            request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
            logger.info(f"Chat request created: {filename}")
            notifier.reply(
                chat_id,
                f"💭 Got it — thinking about <b>{ticker}</b>...",
                reply_to_message_id=user_message_id,
            )
        except OSError as e:
            logger.error(f"Could not write chat request: {e}")
            notifier.reply(chat_id, "⚠️ Could not create chat request.")
    else:
        # No ticker identified — save the reply but can't create a targeted request
        notifier.reply(
            chat_id,
            "💭 Noted, but I couldn't identify which stock you're asking about. "
            "Try replying to a specific stock alert.",
            reply_to_message_id=user_message_id,
        )


def log_reaction(
    message_id,
    emoji: str,
    added: bool,
    sent_registry: SentAlertsRegistry,
    feedback_store: FeedbackStore,
) -> None:
    """Record a single emoji reaction on a bot message into feedback.jsonl.

    Transport-agnostic: callable from the Discord client's on_raw_reaction_add /
    _remove events (Discord delivers one emoji per event, unlike Telegram's
    new/old reaction-set diff). Looks up the alert context by message id; ignores
    reactions on non-alert messages and emojis outside the vocabulary.
    """
    if emoji not in EMOJI_MEANINGS:
        return
    alert = sent_registry.lookup(message_id)
    if alert is None:
        return  # reaction on a non-alert message — ignore
    now = datetime.now(pytz.timezone(MARKET_TIMEZONE))
    if added:
        feedback_store.log(message_id, emoji, alert, now)
    else:
        removal_alert = dict(alert)
        removal_alert["_removed"] = True
        feedback_store.log(message_id, emoji, removal_alert, now)


