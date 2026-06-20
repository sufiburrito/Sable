"""
Inbound-dispatch tests for the Discord migration:
  - command_text() accepts the configured prefix AND "/", normalising to "/cmd".
  - A command flows through listener._handle to a channel-local reply (the
    channel id is the routing token Sable answers on).
  - log_reaction() feeds data/feedback.jsonl via the sent-alerts registry, and
    ignores out-of-vocabulary emojis and reactions on non-alert messages.
"""
from datetime import datetime

import pytz

from alert_bot import listener
from alert_bot.discord_client import command_text
from alert_bot.feedback import SentAlertsRegistry, FeedbackStore

IST = pytz.timezone("Asia/Kolkata")


# ── command_text ─────────────────────────────────────────────────────────────

def test_command_text_accepts_configured_prefix():
    assert command_text("!analyze BBOX", "!") == "/analyze BBOX"


def test_command_text_accepts_slash_so_help_stays_valid():
    assert command_text("/portfolio list", "!") == "/portfolio list"


def test_command_text_ignores_plain_chatter():
    assert command_text("just thinking out loud", "!") is None


def test_command_text_ignores_empty_command_body():
    assert command_text("!", "!") is None
    assert command_text("/", "!") is None


# ── command dispatch → channel-local reply ───────────────────────────────────

class _FakeNotifier:
    def __init__(self):
        self.replies = []

    def reply(self, channel, text, **kwargs):
        self.replies.append((channel, text))
        return 1

    def send(self, *a, **k):
        return 1


def test_handle_help_replies_in_addressed_channel():
    notifier = _FakeNotifier()
    # /help only needs parts/chat_id/notifier; state + custom_store are unused here.
    listener._handle("/help", 5550, notifier, state=None, custom_store=None)
    assert notifier.replies, "expected a reply"
    channel, text = notifier.replies[0]
    assert channel == 5550          # answered in the channel it was addressed in
    assert text                      # non-empty help body


# ── reaction → feedback pipeline ──────────────────────────────────────────────

def _seed_alert(tmp_path, message_id=42):
    registry = SentAlertsRegistry(tmp_path / "sent_alerts.json")
    registry.register(
        message_id=message_id, ticker="SUVEN", alert_type="BUY",
        price_str="₹135", price=135.0, signal="🟢", confidence=2,
        message="Add here.", source="claude", fired_at=datetime.now(IST),
    )
    return registry


def _feedback_lines(store_path):
    if not store_path.exists():
        return []
    return [ln for ln in store_path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_log_reaction_writes_feedback_for_known_alert(tmp_path):
    registry = _seed_alert(tmp_path)
    fb_path = tmp_path / "feedback.jsonl"
    store = FeedbackStore(fb_path)

    listener.log_reaction(42, "👍", True, registry, store)

    lines = _feedback_lines(fb_path)
    assert len(lines) == 1
    assert "SUVEN" in lines[0] and "action_taken" in lines[0]


def test_log_reaction_ignores_unknown_emoji(tmp_path):
    registry = _seed_alert(tmp_path)
    fb_path = tmp_path / "feedback.jsonl"
    store = FeedbackStore(fb_path)

    listener.log_reaction(42, "🦄", True, registry, store)

    assert _feedback_lines(fb_path) == []


def test_log_reaction_ignores_non_alert_message(tmp_path):
    registry = _seed_alert(tmp_path)            # only message_id 42 is registered
    fb_path = tmp_path / "feedback.jsonl"
    store = FeedbackStore(fb_path)

    listener.log_reaction(9999, "👍", True, registry, store)

    assert _feedback_lines(fb_path) == []
