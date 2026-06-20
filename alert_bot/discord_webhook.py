"""
discord_webhook.py — outbound posting for OUT-OF-PROCESS senders.

A single Discord bot token allows exactly one live gateway connection, which the
running bot already holds (discord_client.py). Separate processes — send_message.py,
send_report.py, and the subprocess/curl callers in backtest_levels.py,
process_insider_trades.py, run_forever.sh — therefore cannot use the bot client.
They post into #sable-broadcast via a channel webhook: a stateless HTTP endpoint,
no gateway, no token. Reactions on webhook-posted messages are still seen by the
in-channel bot, so the feedback loop is unaffected.

Uses only `requests`, mirroring the old TelegramNotifier transport so these scripts
stay dependency-light. Formatting is translated to Discord Markdown via the same
html_to_markdown() used by the in-process notifier.
"""
import json
import logging
import os
import time
from pathlib import Path

import requests

from .config import DISCORD_BROADCAST_WEBHOOK, DISCORD_OUTBOX_DIR
from .discord_notifier import html_to_markdown, split_for_discord

logger = logging.getLogger(__name__)

_TIMEOUT = 10


def enqueue(channel_id: int, text: str) -> bool:
    """Drop a {channel_id, content} file in the outbox for the running bot to post
    to that specific channel over its gateway (a broadcast-only webhook can't reach
    #sable-chat). Out-of-process entry point for channel-targeted replies. Returns
    True if the file was written.
    """
    try:
        DISCORD_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        # Unique, time-ordered name; pid + counter avoid collisions within a tick.
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{os.getpid()}_{time.time_ns() % 1_000_000}.json"
        (DISCORD_OUTBOX_DIR / name).write_text(
            json.dumps({"channel_id": int(channel_id), "content": text}),
            encoding="utf-8",
        )
        return True
    except (OSError, ValueError) as e:
        logger.error(f"Discord outbox enqueue failed: {e}")
        return False


def post(text: str, webhook_url: str | None = None) -> int | None:
    """Post a message to #sable-broadcast. Returns the last chunk's message id.

    Long content (digests, convergence reports) is split into Discord's 2000-char
    chunks. `?wait=true` makes Discord return the created message object so we can
    recover its snowflake id (kept as a string in sent_alerts.json).
    """
    url = webhook_url or DISCORD_BROADCAST_WEBHOOK
    if not url:
        logger.error("DISCORD_BROADCAST_WEBHOOK not set — message dropped.")
        return None
    last_id = None
    try:
        for chunk in split_for_discord(html_to_markdown(text)):
            resp = requests.post(
                url,
                params={"wait": "true"},
                json={"content": chunk},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            last_id = resp.json().get("id")
        return last_id
    except requests.RequestException as e:
        logger.error(f"Discord webhook post failed: {e}")
        return None


def post_document(file_path, caption: str = "", webhook_url: str | None = None) -> bool:
    """Upload a file (e.g. a PDF report) to #sable-broadcast. Returns True on success."""
    url = webhook_url or DISCORD_BROADCAST_WEBHOOK
    if not url:
        logger.error("DISCORD_BROADCAST_WEBHOOK not set — document dropped.")
        return False
    path = Path(file_path)
    try:
        with path.open("rb") as f:
            resp = requests.post(
                url,
                data={"content": html_to_markdown(caption) if caption else ""},
                files={"file": (path.name, f)},
                timeout=60,
            )
        resp.raise_for_status()
        return True
    except (requests.RequestException, OSError) as e:
        logger.error(f"Discord webhook document post failed: {e}")
        return False
