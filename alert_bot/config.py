import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Discord I/O (Sable's chat surface — replaces Telegram) ──────────────────
# One bot token = one gateway connection (shared with discord_ingest routing).
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
# #sable-broadcast — everything Sable initiates (alerts, digest, reminders, MMI pin)
DISCORD_BROADCAST_CHANNEL = int(os.getenv("DISCORD_BROADCAST_CHANNEL", "0"))
# #sable-chat — the user's space to start a conversation with Sable
DISCORD_CHAT_CHANNEL = int(os.getenv("DISCORD_CHAT_CHANNEL", "0"))
# Webhook into #sable-broadcast for OUT-OF-PROCESS senders that can't share the
# gateway (send_message.py, send_report.py, subprocess callers, run_forever.sh).
DISCORD_BROADCAST_WEBHOOK = os.getenv("DISCORD_BROADCAST_WEBHOOK", "")
# Leading character for text-parsed commands in Sable's channels (e.g. "!analyze BBOX").
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
# Outbox relay: out-of-process producers drop {channel_id, content} JSON here; the
# running bot posts each to its channel over the gateway (lets the loop answer in
# #sable-chat, which a broadcast-only webhook can't reach).
DISCORD_OUTBOX_DIR = Path(__file__).parent.parent / "data" / "discord_outbox"

# Directory containing stock .md files
STOCKS_DIR = Path(os.getenv("STOCKS_DIR", Path(__file__).parent.parent / "stocks"))

# These .md files in STOCKS_DIR are NOT stock configs
EXCLUDED_MD_FILES = {"_TEMPLATE.md"}

# Polling and cooldown
# Poll interval when Kite WebSocket is active (real-time feed)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
# Poll interval when running on yfinance fallback (avoids rate-limiting)
POLL_INTERVAL_FALLBACK_SECONDS = int(os.getenv("POLL_INTERVAL_FALLBACK_SECONDS", "180"))
COOLDOWN_MINUTES = 30                 # silence same level for 30 min after firing
SPECIAL_ALERT_COOLDOWN_DAYS = 1       # fire calendar alerts at most once per day

# NSE market hours (IST)
MARKET_OPEN = (9, 15)    # (hour, minute)
MARKET_CLOSE = (15, 30)
MARKET_TIMEZONE = "Asia/Kolkata"

# Where the bot persists its cooldown/direction state between restarts
STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"

# Custom user-defined alerts added via Telegram /alert command
CUSTOM_ALERTS_FILE = Path(__file__).parent.parent / "data" / "custom_alerts.json"

# Append-only log of every alert that fires (read by the TUI)
ALERTS_LOG = Path(__file__).parent.parent / "data" / "alerts.jsonl"

# File-based queue for autonomous analysis requests (written by bot, read by /loop)
REQUESTS_DIR = Path(__file__).parent.parent / "requests"

# Staging area for --no-update analysis drafts (reviewed manually before rotation)
NIGHTLY_LEVELS_DIR = Path(__file__).parent.parent / "nightly-levels"

# Reaction feedback: message_id registry and reaction log
SENT_ALERTS_FILE = Path(__file__).parent.parent / "data" / "sent_alerts.json"
FEEDBACK_LOG     = Path(__file__).parent.parent / "data" / "feedback.jsonl"

# Conversation log: user replies to bot messages
CONVERSATIONS_LOG = Path(__file__).parent.parent / "data" / "conversations.jsonl"

# Approach alerts — "getting close to a level" nudges for quiet stocks
# Only fires for stocks where the gap between nearest BUY and SELL exceeds this %
APPROACH_DEAD_ZONE_PCT = 12.0
# ... AND fewer than this many alerts fired in the last 30 days
APPROACH_MAX_RECENT_ALERTS = 5
# Proximity trigger: fire when price is within this many ATRs of a level
APPROACH_ATR_MULTIPLIER = 1.0
# Cooldown: one approach alert per level per this many hours
APPROACH_COOLDOWN_HOURS = 24

# CalDAV calendar server (Radicale) — subscribe over Tailscale
CALDAV_PORT          = int(os.getenv("CALDAV_PORT", "5232"))
CALDAV_INI           = Path(__file__).parent.parent / "config" / "radicale.ini"
CALDAV_STORAGE_DIR   = Path(__file__).parent.parent / "data" / "calendar"
CALDAV_CALENDAR_NAME = os.getenv("CALDAV_CALENDAR_NAME", "algotrading-events")
CALDAV_USER          = os.getenv("CALDAV_USER", "stock")  # collection lives under /user/

# Dalal Street morning digest — community market commentary
DIGEST_DIR = Path(__file__).parent.parent / "dalalstreet_morning"

# Nightly refresh — auto-generate analysis requests after each trading day
NIGHTLY_REFRESH_FILE = Path(__file__).parent.parent / "config" / "active_refresh_stocks.txt"
NIGHTLY_REFRESH_HOUR = 23   # 11 PM IST
NIGHTLY_REFRESH_MODE = "chart-news-community-retro-backtest-forecast"

# Commodities — currently just gold (Phase 1)
COMMODITIES_DIR = Path(os.getenv("COMMODITIES_DIR", Path(__file__).parent.parent / "commodities"))
GOLD_CONFIG_FILE = COMMODITIES_DIR / "gold.md"

# Gold tracker outputs (Python writes both)
GOLD_SNAPSHOT_FILE = Path(__file__).parent.parent / "data" / "gold_snapshot.json"
GOLD_BUNDLE_FILE = Path(__file__).parent.parent / "data" / "gold_analysis_bundle.json"
# Autonomous-loop-managed narrative file (read by Python, written by Sable)
GOLD_NARRATIVE_FILE = Path(__file__).parent.parent / "data" / "gold_narrative.json"

# ── Zerodha Kite Connect (all vars optional; omit to run in yfinance-only mode) ──
ZERODHA_API_KEY      = os.getenv("ZERODHA_API_KEY", "")
ZERODHA_API_SECRET   = os.getenv("ZERODHA_API_SECRET", "")
ZERODHA_USER_ID      = os.getenv("ZERODHA_USER_ID", "")
ZERODHA_PASSWORD     = os.getenv("ZERODHA_PASSWORD", "")
ZERODHA_TOTP_SECRET  = os.getenv("ZERODHA_TOTP_SECRET", "")
KITE_TOKEN_FILE      = Path(os.getenv("KITE_TOKEN_FILE", Path(__file__).parent.parent / "data" / "kite_token.json"))

# ── Groww Trade API (optional; omit to run without Groww) ──────────────────────
GROWW_API_KEY     = os.getenv("GROWW_API_KEY", "")
GROWW_TOTP_SECRET = os.getenv("GROWW_TOTP_SECRET", "")
GROWW_TOKEN_FILE  = Path(os.getenv(
    "GROWW_TOKEN_FILE",
    Path(__file__).parent.parent / "data" / "groww_token.json"
))
