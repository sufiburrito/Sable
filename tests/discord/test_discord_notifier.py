"""
Transport-layer tests for the Discord migration:
  - html_to_markdown() translates the HTML subset the codebase emits.
  - DiscordNotifier bridges sync callers into the client's asyncio loop, applies
    the Markdown translation at the send boundary, and fails silently when the
    loop is unavailable (the alert-hot-path contract).
"""
import asyncio
import threading

import pytest

from alert_bot.discord_notifier import (
    DiscordNotifier, html_to_markdown, split_for_discord, DISCORD_LIMIT,
)


# ── html_to_markdown ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("html,expected", [
    ("<b>BUY HBLENGINE</b> @ ₹613", "**BUY HBLENGINE** @ ₹613"),
    ("<i>Sable — I like this.</i>", "*Sable — I like this.*"),
    ("Use <code>/analyze SUVEN</code>", "Use `/analyze SUVEN`"),
    ('See <a href="https://x.com">link</a>', "See [link](https://x.com)"),
    ("AT&amp;T and 5 &lt; 10", "AT&T and 5 < 10"),
    ("plain text", "plain text"),
    ("", ""),
])
def test_html_to_markdown(html, expected):
    assert html_to_markdown(html) == expected


def test_unknown_tags_are_stripped_not_shown():
    assert html_to_markdown("a <u>underline</u> b") == "a underline b"


# ── split_for_discord (Discord's 2000-char limit) ────────────────────────────

def test_short_text_is_one_chunk():
    assert split_for_discord("hello") == ["hello"]


def test_long_text_splits_under_limit_on_newlines():
    text = "\n".join(["line %d" % i for i in range(1000)])  # well over 2000 chars
    chunks = split_for_discord(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    # Reassembling the chunks restores the original content (newline-joined).
    assert "\n".join(chunks) == text


def test_single_giant_line_is_hard_split():
    chunks = split_for_discord("x" * 5000)
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert "".join(chunks) == "x" * 5000


# ── DiscordNotifier silent-failure contract ──────────────────────────────────

def test_send_returns_none_when_loop_missing():
    n = DiscordNotifier(client=None, loop=None, broadcast_channel_id=123)
    assert n.send("hello") is None
    assert n.test_connection() is False


def test_send_returns_none_when_loop_not_running():
    class DeadLoop:
        def is_running(self):
            return False
    n = DiscordNotifier(client=object(), loop=DeadLoop(), broadcast_channel_id=123)
    assert n.send("hello") is None


# ── DiscordNotifier happy-path bridge ─────────────────────────────────────────

class _FakeMessage:
    id = 999


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return _FakeMessage()


class _FakeClient:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, cid):
        return self._channel


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)


def test_send_bridges_and_translates(running_loop):
    channel = _FakeChannel()
    n = DiscordNotifier(_FakeClient(channel), running_loop, broadcast_channel_id=123)
    mid = n.send("<b>BUY</b> X")
    assert mid == 999
    # HTML was translated to Markdown at the send boundary.
    assert channel.sent == ["**BUY** X"]


def test_reply_chunks_long_text(running_loop):
    # All command responses go through reply(); a >2000-char reply must be split,
    # not rejected by Discord (regression: the 400 "Must be 2000 or fewer" bug).
    channel = _FakeChannel()

    class ChannelClient:
        def get_channel(self, cid):
            return channel
    n = DiscordNotifier(ChannelClient(), running_loop, broadcast_channel_id=123)
    long_text = "\n".join("row %d" % i for i in range(900))
    n.reply(123, long_text)
    assert len(channel.sent) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in channel.sent)


def test_send_returns_none_when_channel_unavailable(running_loop):
    class NoChannelClient:
        def get_channel(self, cid):
            return None
    n = DiscordNotifier(NoChannelClient(), running_loop, broadcast_channel_id=123)
    assert n.send("anything") is None
