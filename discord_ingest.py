"""
Discord ingest bot — watches channels on a private Discord server and saves
forwarded messages as files for the autonomous loop to process.

Channel → directory routing:
  #dalal-digest   → dalalstreet_morning/YYYY-MM-DD.md
  #insider-info   → insider_trades/YYYY-MM-DD.md
  #general-intel  → intel_inbox/YYYY-MM-DD_HHMMSS.md

Debounce: messages are saved with a .pending extension first. After 5 minutes
of silence in a channel, the file is renamed to .md — only then does the
autonomous loop see it. This prevents partial digests from being processed
while you're still forwarding messages.

Run alongside the main bot:
  python3 discord_ingest.py

Requires DISCORD_BOT_TOKEN and channel IDs in .env.
"""
import asyncio
import logging
import os
from pathlib import Path

import discord
import pytz
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ── Config ────────────────────────────────────────────────────────────────

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# How long to wait after the last message before finalising the file.
DEBOUNCE_SECONDS = 5 * 60  # 5 minutes

# Channel ID → (target directory, filename pattern)
# Patterns: "date" = YYYY-MM-DD.md (one file per day, appends if multiple messages)
#           "timestamp" = YYYY-MM-DD_HHMMSS.md (unique per message)
CHANNEL_ROUTES: dict[int, tuple[Path, str]] = {
    int(os.getenv("DISCORD_CHANNEL_DALAL_DIGEST", "0")): (
        Path("dalalstreet_morning"), "date"
    ),
    int(os.getenv("DISCORD_CHANNEL_INSIDER_INFO", "0")): (
        Path("insider_trades"), "date"
    ),
    int(os.getenv("DISCORD_CHANNEL_GENERAL_INTEL", "0")): (
        Path("intel_inbox"), "timestamp"
    ),
}

# ── Debounce state ────────────────────────────────────────────────────────

# Maps a pending filepath (e.g. dalalstreet_morning/2026-04-27.md.pending)
# to its active debounce timer task. When a new message arrives for the same
# file, the old timer is cancelled and a fresh one starts.
_pending_timers: dict[Path, asyncio.Task] = {}


async def _finalise_after_delay(pending_path: Path):
    """Wait for the debounce period, then rename .pending → .md."""
    await asyncio.sleep(DEBOUNCE_SECONDS)

    final_path = pending_path.with_suffix("")  # strip .pending → .md
    pending_path.rename(final_path)
    _pending_timers.pop(pending_path, None)

    logger.info(
        f"Finalised: {final_path} "
        f"({final_path.stat().st_size} bytes, ready for processing)"
    )


def _reset_debounce(pending_path: Path):
    """Cancel any existing timer for this file and start a fresh one."""
    old_task = _pending_timers.get(pending_path)
    if old_task is not None:
        old_task.cancel()

    _pending_timers[pending_path] = asyncio.create_task(
        _finalise_after_delay(pending_path)
    )


# ── Bot setup ─────────────────────────────────────────────────────────────

# We only need to read message content — minimal permissions.
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    """Log which channels we're watching when the bot connects."""
    logger.info(f"Discord ingest bot connected as {client.user}")
    for channel_id, (target_dir, pattern) in CHANNEL_ROUTES.items():
        ch = client.get_channel(channel_id)
        name = ch.name if ch else "unknown"
        logger.info(f"  #{name} ({channel_id}) → {target_dir}/ ({pattern})")


@client.event
async def on_message(message: discord.Message):
    """Standalone entrypoint: save messages from watched channels (debounced)."""
    if message.author == client.user:
        return
    await route_message(message)


async def route_message(message: "discord.Message") -> bool:
    """Save a watched-channel message as a .pending file (debounced).

    Returns True if the message belonged to a watched ingest channel (and was
    handled), False otherwise. Extracted so the unified Sable client
    (alert_bot/discord_client.py) can reuse ingest routing over the SAME gateway
    connection — one bot token permits only one connection.
    """
    # Check if this channel is one we're watching
    route = CHANNEL_ROUTES.get(message.channel.id)
    if route is None:
        return False

    target_dir, pattern = route

    # Build the file content from the message
    content = message.content or ""

    # Discord "Forward" feature: original text + attachments live in
    # message.message_snapshots (forwarder's own .content is usually empty).
    for snap in getattr(message, "message_snapshots", None) or []:
        snap_content = getattr(snap, "content", "") or ""
        if snap_content:
            content += ("\n\n" if content else "") + snap_content
        for attachment in getattr(snap, "attachments", None) or []:
            if attachment.filename.endswith((".md", ".txt", ".csv")):
                try:
                    file_bytes = await attachment.read()
                    content += f"\n\n--- {attachment.filename} ---\n"
                    content += file_bytes.decode("utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"Could not read forwarded attachment {attachment.filename}: {e}")

    # If the message has attachments (e.g. .md or .csv files), append their
    # text content below the message body.
    for attachment in message.attachments:
        if attachment.filename.endswith((".md", ".txt", ".csv")):
            try:
                file_bytes = await attachment.read()
                content += f"\n\n--- {attachment.filename} ---\n"
                content += file_bytes.decode("utf-8", errors="replace")
            except Exception as e:
                logger.warning(f"Could not read attachment {attachment.filename}: {e}")

    if not content.strip():
        logger.debug(f"Empty message in #{message.channel.name}, skipping.")
        return True

    # Convert message timestamp (UTC) to IST for the filename
    msg_ist = message.created_at.astimezone(IST)

    if pattern == "date":
        filename = f"{msg_ist.strftime('%Y-%m-%d')}.md"
    else:  # "timestamp" — unique per message, no debounce needed
        filename = f"{msg_ist.strftime('%Y-%m-%d_%H%M%S')}.md"

    target_dir.mkdir(parents=True, exist_ok=True)

    # ── Timestamp pattern: save directly (each message is its own file) ──
    if pattern == "timestamp":
        filepath = target_dir / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info(
            f"Saved: #{message.channel.name} → {filepath} "
            f"({len(content)} chars, from {message.author.display_name})"
        )
        return True

    # ── Date pattern: save as .pending, debounce before finalising ───────
    pending_path = target_dir / (filename + ".pending")

    # Append to existing .pending file (multiple forwarded messages = one digest)
    if pending_path.exists():
        existing = pending_path.read_text(encoding="utf-8")
        content = existing + "\n\n" + content
        logger.info(f"Appending to {pending_path} (debounce reset)")
    else:
        # Check if a finalised .md already exists for today (e.g. bot restarted)
        final_path = target_dir / filename
        if final_path.exists():
            existing = final_path.read_text(encoding="utf-8")
            content = existing + "\n\n" + content
            logger.info(f"Re-opening {final_path} as pending (new message arrived)")
            final_path.unlink()  # remove .md, will be re-created by debounce
        else:
            logger.info(f"New pending file: {pending_path}")

    pending_path.write_text(content, encoding="utf-8")
    logger.info(
        f"Buffered: #{message.channel.name} → {pending_path} "
        f"({len(content)} chars, from {message.author.display_name}) "
        f"— finalises in {DEBOUNCE_SECONDS // 60}min if no more messages"
    )

    # Reset the debounce timer for this file
    _reset_debounce(pending_path)
    return True


def _run_bot():
    """Entry point for the Discord bot (runs its own asyncio event loop)."""
    # Remove unconfigured channel routes
    if 0 in CHANNEL_ROUTES:
        del CHANNEL_ROUTES[0]
        logger.warning("Some channel IDs are missing from .env — those routes are disabled.")

    client.run(TOKEN)


def start():
    """Start the Discord ingest bot in a daemon thread.

    Called from alert_bot/main.py during startup, same pattern as the
    Telegram command listener. The daemon flag means this thread dies
    automatically when the main process exits.
    """
    if not TOKEN:
        logger.warning("Discord ingest: DISCORD_BOT_TOKEN not set, skipping.")
        return

    import threading
    thread = threading.Thread(
        target=_run_bot,
        daemon=True,
        name="discord-ingest",
    )
    thread.start()
    logger.info("Discord ingest bot started in background thread.")


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN not set in .env")
        raise SystemExit(1)

    _run_bot()
