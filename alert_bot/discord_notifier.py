"""
discord_notifier.py — Sable's outbound transport for the IN-PROCESS bot.

Mirrors the public surface of TelegramNotifier so that main.py and the listener
command handlers barely change. Each method bridges from synchronous caller code
into the single asyncio event loop owned by the Discord client (which runs in its
own daemon thread — see discord_client.py). One bot token permits exactly one
gateway connection, so OUT-OF-PROCESS senders use discord_webhook.py instead.

Design contracts carried over from the Telegram era:
  - send() returns the new message id (Discord snowflake, as int) or None on failure.
    sent_alerts.json keys it as a string, exactly like the old Telegram message_id.
  - Every method fails SILENTLY (logs + returns None/False). A transport hiccup must
    never raise into the alert hot path.

Formatting: the rest of the codebase emits Telegram HTML (<b>, <i>, <code>, <a>).
html_to_markdown() translates that to Discord Markdown once, here at the send
boundary — so the ~211 existing HTML strings stay untouched.
"""
import asyncio
import html as _html
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# How long a bridged coroutine may run before we give up (seconds). Keeps the
# synchronous caller from blocking forever if the gateway is wedged.
_BRIDGE_TIMEOUT = 15


# ── HTML → Discord Markdown ─────────────────────────────────────────────────

_LINK_RE = re.compile(r'<a\s+href="([^"]*)"\s*>(.*?)</a>', re.DOTALL | re.IGNORECASE)


def html_to_markdown(text: str) -> str:
    """Translate the small subset of Telegram HTML we emit into Discord Markdown.

    Handled tags: <b>/<strong> → **bold**, <i>/<em> → *italic*,
    <code>/<pre> → `code`, <a href="u">t</a> → [t](u). HTML entities are
    unescaped last (so &amp;/&lt;/&gt; render as &, <, >). Unknown tags are
    stripped rather than shown literally.
    """
    if not text:
        return text

    # Links first (they wrap inner text we don't want to re-process as tags).
    text = _LINK_RE.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", text)

    # Bold / italic / code. Discord has no <u>; underline maps to nothing special.
    text = re.sub(r"</?(?:b|strong)\s*>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:i|em)\s*>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:code|pre)\s*>", "`", text, flags=re.IGNORECASE)

    # Drop any other tags we didn't explicitly translate (e.g. stray <u>).
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)

    # Telegram required &amp; etc.; Discord wants the literal characters.
    text = _html.unescape(text)
    return text


# ── Length splitting ─────────────────────────────────────────────────────────

# Discord's per-message content limit is 2000 chars (Telegram allowed 4096). The
# morning digest / convergence reports run longer, so split on line boundaries.
DISCORD_LIMIT = 2000


def split_for_discord(text, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text into <=limit-char chunks, preferring newline boundaries.

    A single over-long line is hard-split. Returns at least one chunk (possibly
    empty) so callers can always iterate.
    """
    if text is None:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                 # a single giant line
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:limit]); line = line[limit:]
        add = (("\n" + line) if cur else line)
        if len(cur) + len(add) > limit:
            chunks.append(cur); cur = line
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks or [""]


# ── Notifier ────────────────────────────────────────────────────────────────

class DiscordNotifier:
    """Outbound sender bound to the running Discord client + its event loop.

    Parameters
    ----------
    client : discord.Client
        The single live client (also doing ingest + command dispatch).
    loop : asyncio.AbstractEventLoop
        That client's event loop (it runs in a daemon thread).
    broadcast_channel_id : int
        Default target for unsolicited messages (#sable-broadcast).
    """

    def __init__(self, client, loop, broadcast_channel_id: int):
        self._client = client
        self._loop = loop
        self._broadcast_id = broadcast_channel_id

    # ── cross-thread bridge ──────────────────────────────────────────────
    def _run(self, coro):
        """Run an async coroutine on the client's loop from sync code.

        Returns the coroutine's result, or None on any failure (timeout, loop
        not running, Discord error). Never raises — the alert hot path depends
        on that contract.
        """
        if self._loop is None or not self._loop.is_running():
            logger.error("Discord loop not running — message dropped.")
            coro.close()  # avoid an un-awaited-coroutine warning
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=_BRIDGE_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — silent-failure contract
            logger.error(f"Discord send failed: {e}")
            return None

    def _resolve_channel(self, channel):
        """Accept a channel object or an int id; return a channel or None."""
        if channel is None:
            channel = self._broadcast_id
        if isinstance(channel, int):
            return self._client.get_channel(channel)
        return channel  # already a channel object

    # ── public API (mirrors TelegramNotifier) ────────────────────────────
    def send(self, message: str, channel=None) -> int | None:
        """Post a message to #sable-broadcast (or `channel`). Returns message id."""
        async def _do():
            ch = self._resolve_channel(channel)
            if ch is None:
                logger.error("Discord channel unavailable — message dropped.")
                return None
            last_id = None
            for chunk in split_for_discord(html_to_markdown(message)):
                msg = await ch.send(chunk)
                last_id = msg.id
            return last_id
        return self._run(_do())

    def reply(self, channel, text: str, reply_to_message_id=None) -> int | None:
        """Post `text` to `channel` (channel-local reply rule). `channel` is the
        originating channel id/object so Sable answers where she was addressed.
        Returns the (last chunk's) message id."""
        async def _do():
            ch = self._resolve_channel(channel)
            if ch is None:
                return None
            chunks = split_for_discord(html_to_markdown(text))
            last_id = None
            for i, chunk in enumerate(chunks):
                kwargs = {}
                if i == 0 and reply_to_message_id is not None:
                    kwargs["reference"] = ch.get_partial_message(int(reply_to_message_id))
                msg = await ch.send(chunk, **kwargs)
                last_id = msg.id
            return last_id
        return self._run(_do())

    def send_many(self, messages: list[str], channel=None) -> None:
        for msg in messages:
            self.send(msg, channel=channel)

    def edit_message(self, message_id: int, text: str, channel=None) -> bool:
        """Edit a previously sent message in place. Returns True on success."""
        async def _do():
            ch = self._resolve_channel(channel)
            if ch is None:
                return False
            msg = await ch.fetch_message(int(message_id))
            content = html_to_markdown(text)
            if len(content) > DISCORD_LIMIT:  # an edit is one message — can't split
                content = content[:DISCORD_LIMIT - 1] + "…"
            await msg.edit(content=content)
            return True
        result = self._run(_do())
        return bool(result)

    def pin_message(self, message_id: int, channel=None) -> None:
        async def _do():
            ch = self._resolve_channel(channel)
            if ch is None:
                return None
            msg = await ch.fetch_message(int(message_id))
            await msg.pin()
            return True
        self._run(_do())

    def send_document(self, file_path, caption: str = "", channel=None) -> bool:
        """Upload a file (e.g. a PDF report) to #sable-broadcast (or `channel`)."""
        import discord
        path = Path(file_path)

        async def _do():
            ch = self._resolve_channel(channel)
            if ch is None:
                return False
            await ch.send(
                content=html_to_markdown(caption) if caption else None,
                file=discord.File(str(path), filename=path.name),
            )
            return True
        result = self._run(_do())
        return bool(result)

    def test_connection(self) -> bool:
        """Startup ping into #sable-broadcast. Returns True on success."""
        ok = self.send("Sable is now running.")
        if ok:
            logger.info("Discord connection confirmed.")
        else:
            logger.error("Could not post to Discord — check token and channel IDs.")
        return bool(ok)
