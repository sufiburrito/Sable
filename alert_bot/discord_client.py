"""
discord_client.py — Sable's single unified Discord gateway connection.

One bot token permits exactly one gateway connection; this module owns it and does
three jobs over it:
  1. Ingest routing for the existing #dalal-digest / #insider-info / #general-intel
     channels — delegated to discord_ingest.route_message (no logic duplicated).
  2. Inbound commands + alert-replies in #sable-broadcast / #sable-chat — dispatched
     to the existing listener.py handlers. Sable answers in the SAME channel she was
     addressed in (the channel-local reply rule).
  3. Emoji reactions → feedback logging (listener.log_reaction → data/feedback.jsonl)
     and the ✅/❌ /portfolio-archive confirmation (reaction-based, not buttons —
     buttons proved unreliable across our cross-thread send bridge).

Outbound posting from synchronous bot code goes through DiscordNotifier
(discord_notifier.py), which bridges into THIS client's event loop. Out-of-process
senders use discord_webhook.py instead.

Concurrency note: the listener command handlers are synchronous and call
notifier.reply(), which bridges back into this loop via run_coroutine_threadsafe.
Running a handler directly on the loop thread would therefore DEADLOCK. We dispatch
every handler through asyncio.to_thread() so the loop stays free to service those
bridged sends.
"""
import asyncio
import json
import logging
import threading

import discord

from .config import (
    DISCORD_BOT_TOKEN, DISCORD_BROADCAST_CHANNEL, DISCORD_CHAT_CHANNEL,
    COMMAND_PREFIX, DISCORD_OUTBOX_DIR,
)
from . import listener as _listener

logger = logging.getLogger(__name__)

# Channels where Sable accepts commands + replies (and answers in-channel).
_SABLE_CHANNELS = {cid for cid in (DISCORD_BROADCAST_CHANNEL, DISCORD_CHAT_CHANNEL) if cid}

# Filled in by configure() once main.py has built the notifier + stores. Until
# then, Sable-channel messages are ignored (ingest still works).
_ctx: dict = {
    "notifier": None, "state": None, "custom_store": None,
    "sent_registry": None, "feedback_store": None, "convo_store": None,
}

intents = discord.Intents.default()      # default() already includes reactions
intents.message_content = True
client = discord.Client(intents=intents)

_ready = threading.Event()
_outbox_started = False
_OUTBOX_POLL_SECONDS = 3

# Reaction-based /portfolio archive confirmation: message_id → (ticker, channel_id).
# Reactions are used instead of buttons because discord.ui Views rely on a fragile
# in-memory dispatch registry that doesn't survive our cross-thread send bridge or a
# restart, whereas reaction events (on_raw_reaction_add) are delivered reliably.
_pending_archive: dict = {}


def command_text(content: str, prefix: str) -> str | None:
    """Normalise a chat message into the "/cmd …" string the listener expects,
    or None if it isn't a command.

    Accepts the configured prefix AND a literal "/", so the existing help text
    (which shows "/portfolio …") stays accurate while the user's chosen prefix
    also works. Returns None for an empty command body too.
    """
    content = (content or "").strip()
    used = None
    if prefix and content.startswith(prefix):
        used = prefix
    elif content.startswith("/"):
        used = "/"
    if used is None:
        return None
    body = content[len(used):].lstrip()
    if not body:
        return None
    return "/" + body


def configure(*, notifier, state, custom_store, sent_registry, feedback_store, convo_store):
    """Wire command/reaction handling to the live stores. Idempotent."""
    _ctx.update(
        notifier=notifier, state=state, custom_store=custom_store,
        sent_registry=sent_registry, feedback_store=feedback_store,
        convo_store=convo_store,
    )
    logger.info("Discord command dispatch configured.")


# ── Gateway events ───────────────────────────────────────────────────────────

@client.event
async def on_ready():
    global _outbox_started
    logger.info(f"Sable Discord client connected as {client.user}")
    for cid in _SABLE_CHANNELS:
        ch = client.get_channel(cid)
        logger.info(f"  watching #{getattr(ch, 'name', '?')} ({cid})")
    # Start the outbox relay once (on_ready can fire again on reconnect).
    if not _outbox_started:
        _outbox_started = True
        client.loop.create_task(_outbox_loop())
        logger.info(f"Outbox relay watching {DISCORD_OUTBOX_DIR}")
    _ready.set()


async def _outbox_loop():
    """Relay out-of-process messages to their channel over the gateway.

    Out-of-process producers (the loop's chat/note replies) drop a
    {channel_id, content} JSON file in DISCORD_OUTBOX_DIR; we post it to that
    channel and delete the file. A broadcast-only webhook can't reach #sable-chat,
    so this is how Sable answers in the channel she was addressed in. Never raises
    out of the loop — a bad file is renamed .failed and skipped.
    """
    from .discord_notifier import html_to_markdown, split_for_discord
    while True:
        try:
            if DISCORD_OUTBOX_DIR.exists():
                for f in sorted(DISCORD_OUTBOX_DIR.glob("*.json")):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        ch = client.get_channel(int(data["channel_id"]))
                        content = data.get("content", "")
                        if ch is not None and content:
                            for chunk in split_for_discord(html_to_markdown(content)):
                                await ch.send(chunk)
                        f.unlink()
                    except Exception as e:
                        logger.error(f"Outbox relay failed for {f.name}: {e}")
                        try:
                            f.rename(f.with_suffix(".failed"))
                        except OSError:
                            pass
        except Exception as e:
            logger.error(f"Outbox loop error: {e}")
        await asyncio.sleep(_OUTBOX_POLL_SECONDS)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    # 1) Ingest channels — reuse existing routing over this one connection.
    try:
        from discord_ingest import route_message
        if await route_message(message):
            return
    except Exception as e:  # ingest must never break command handling
        logger.error(f"Ingest routing failed: {e}")

    # 2) Sable channels only.
    if message.channel.id not in _SABLE_CHANNELS:
        return
    notifier = _ctx["notifier"]
    if notifier is None:
        return  # not configured yet

    content = (message.content or "").strip()
    if not content:
        return

    # channel id is the routing token the handlers pass straight to notifier.reply,
    # so Sable answers in the channel she was addressed in.
    channel_id = message.channel.id

    text = command_text(content, COMMAND_PREFIX)
    if text is not None:
        await asyncio.to_thread(_safe_handle, text, channel_id, notifier)
        return

    # 3) A reply to one of Sable's messages → conversational chat handler.
    ref = message.reference
    if ref is not None and ref.message_id:
        ref_text = ""
        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            ref_text = resolved.content or ""
        else:
            try:
                fetched = await message.channel.fetch_message(ref.message_id)
                ref_text = fetched.content or ""
            except Exception:
                pass
        original = {"message_id": ref.message_id, "text": ref_text}
        await asyncio.to_thread(
            _safe_reply, content, channel_id, message.id, original, notifier,
        )


@client.event
async def on_raw_reaction_add(payload: "discord.RawReactionActionEvent"):
    await _record_raw_reaction(payload, added=True)


@client.event
async def on_raw_reaction_remove(payload: "discord.RawReactionActionEvent"):
    await _record_raw_reaction(payload, added=False)


# ── Handler wrappers (run off the loop, via to_thread) ───────────────────────

def _safe_handle(text, channel_id, notifier):
    try:
        _listener._handle(
            text, channel_id, notifier, _ctx["state"], _ctx["custom_store"],
            _ctx["feedback_store"], _ctx["convo_store"],
        )
    except Exception as e:
        logger.error(f"Command handling failed for {text!r}: {e}")


def _safe_reply(content, channel_id, user_msg_id, original, notifier):
    try:
        _listener._handle_reply(
            content, channel_id, user_msg_id, original,
            notifier, _ctx["sent_registry"], _ctx["convo_store"],
        )
    except Exception as e:
        logger.error(f"Reply handling failed: {e}")


async def _record_raw_reaction(payload, *, added: bool):
    if client.user is not None and payload.user_id == client.user.id:
        return  # ignore Sable's own reactions (incl. the seeded ✅/❌)
    if payload.channel_id not in _SABLE_CHANNELS:
        return
    emoji = str(payload.emoji)  # unicode emoji → its char; custom → "<:name:id>"

    # /portfolio archive confirmation takes priority over feedback logging.
    if added and payload.message_id in _pending_archive:
        await _handle_archive_reaction(payload.message_id, emoji)
        return

    sent_registry = _ctx["sent_registry"]
    feedback_store = _ctx["feedback_store"]
    if sent_registry is None or feedback_store is None:
        return
    await asyncio.to_thread(
        _listener.log_reaction, payload.message_id, emoji, added,
        sent_registry, feedback_store,
    )


# ── /portfolio archive confirmation (reaction-based) ─────────────────────────
# Buttons (discord.ui.View) proved unreliable here: the click reaches the bot but
# discord.py never dispatches it to the View callback (the in-memory view registry
# doesn't survive our cross-thread send bridge), so the interaction is never ack'd
# and Discord shows "interaction failed". Reactions use on_raw_reaction_add — the
# same proven path as feedback logging — with no interaction token or 3s window.

def register_pending_archive(message_id: int, ticker: str, channel_id: int) -> None:
    """Mark a posted archive-confirm message as pending and seed ✅/❌ reactions on it
    (so the user just taps). Called from listener._portfolio_archive_prompt, which runs
    in a worker thread — the reaction seeding is bridged onto the client loop."""
    _pending_archive[message_id] = (ticker, channel_id)

    async def _seed():
        ch = client.get_channel(channel_id)
        if ch is None:
            return
        msg = ch.get_partial_message(message_id)
        try:
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
        except Exception as e:
            logger.error(f"seeding archive reactions failed: {e}")

    try:
        asyncio.run_coroutine_threadsafe(_seed(), client.loop)
    except Exception as e:
        logger.error(f"register_pending_archive failed: {e}")


async def _handle_archive_reaction(message_id: int, emoji: str) -> None:
    if emoji not in ("✅", "❌"):
        return
    entry = _pending_archive.pop(message_id, None)
    if entry is None:
        return
    ticker, channel_id = entry
    from .discord_notifier import html_to_markdown
    ch = client.get_channel(channel_id)
    if ch is None:
        return
    if emoji == "✅":
        text = await asyncio.to_thread(_listener.perform_archive, ticker)
    else:
        text = _listener.archive_cancel_text(ticker)
    try:
        await ch.get_partial_message(message_id).edit(content=html_to_markdown(text))
    except Exception as e:
        logger.error(f"archive reaction edit failed for {ticker}: {e}")


# ── Lifecycle ────────────────────────────────────────────────────────────────

def start_and_wait(timeout: int = 30):
    """Start the single Discord client in a daemon thread and block until it has
    connected (on_ready fired). Returns (client, loop) for building DiscordNotifier."""
    if not DISCORD_BOT_TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN not set. Copy .env.example → .env and fill in the "
            "Discord vars (token + channel IDs)."
        )

    def _run():
        client.run(DISCORD_BOT_TOKEN, log_handler=None)

    threading.Thread(target=_run, daemon=True, name="sable-discord").start()
    if not _ready.wait(timeout=timeout):
        raise SystemExit("Discord client did not become ready in time.")
    return client, client.loop
