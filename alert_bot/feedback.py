"""
Reaction feedback system.

When the bot sends a price alert, the message_id is registered in
SentAlertsRegistry (data/sent_alerts.json).  When you react to that
message in Telegram, the listener looks up the message_id here and
appends a record to FeedbackStore (data/feedback.jsonl).

Emoji vocabulary:
    👍  action_taken      — I performed this trade/action
    👎  disagree          — This feels like a bad idea
    ⏳  watching          — Good alert, but I'm waiting for better entry
    ✅  profitable        — This trade was profitable in hindsight
    ❌  not_profitable    — This trade was not profitable
    🎯  perfect_call      — Perfect timing and level

Default (no reaction) = no action taken.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Emoji vocabulary
# ---------------------------------------------------------------------------

EMOJI_MEANINGS: dict[str, str] = {
    "👍": "action_taken",
    "👎": "disagree",
    "⏳": "watching",
    "✅": "profitable",
    "❌": "not_profitable",
    "🎯": "perfect_call",
}

# Emojis that represent a positive outcome (used later for calibration)
POSITIVE_OUTCOMES = {"profitable", "perfect_call"}
# Emojis that represent a negative outcome
NEGATIVE_OUTCOMES = {"not_profitable"}
# Emojis that represent user engagement (acted or watching)
ENGAGEMENT = {"action_taken", "watching"}


# ---------------------------------------------------------------------------
# SentAlertsRegistry — maps message_id → alert metadata
# ---------------------------------------------------------------------------

class SentAlertsRegistry:
    """
    Persists data/sent_alerts.json.
    Keys are string message_ids (JSON requires string keys).
    """

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def register(
        self,
        message_id: int,
        ticker: str,
        alert_type: str,
        price_str: str,
        price: Optional[float],
        signal: str,
        confidence: int,
        message: str,
        source: str,           # "claude" | "manual"
        fired_at: datetime,
        confidence_result=None,   # Optional[ConfidenceResult] — fire-time factor vector
    ) -> None:
        """Record a sent alert so reactions can be linked back to it.

        When `confidence_result` is supplied, persist the full per-factor vector
        (name → −1/0/+1) plus composite/max_score/verdict. This is the calibration
        spine's ground-truth log: the IC study (alert_bot/calibrate.py) later joins
        each logged vector to its realized forward return. Old records (and manual
        alerts without a result) simply omit the `factors` key.
        """
        record = {
            "ticker":     ticker,
            "alert_type": alert_type,
            "price_str":  price_str,
            "price":      price,
            "signal":     signal,
            "confidence": confidence,
            "message":    message,
            "source":     source,
            "fired_at":   fired_at.isoformat(),
        }
        if confidence_result is not None:
            record["factors"] = {f.name: f.score for f in confidence_result.factors}
            record["composite"] = confidence_result.composite
            record["max_score"] = confidence_result.max_score
            record["verdict"] = confidence_result.verdict
        self._data[str(message_id)] = record
        self._save()

    def lookup(self, message_id: int) -> Optional[dict]:
        """Return the alert metadata for a message_id, or None."""
        return self._data.get(str(message_id))

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load sent_alerts registry: {e}")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError as e:
            logger.error(f"Could not save sent_alerts registry: {e}")


# ---------------------------------------------------------------------------
# FeedbackStore — append-only reaction log
# ---------------------------------------------------------------------------

class FeedbackStore:
    """
    Appends records to data/feedback.jsonl — one line per reaction event.
    Each record contains the full alert context plus the emoji and its meaning.
    """

    def __init__(self, path: Path):
        self._path = path

    def log(self, message_id: int, emoji: str, alert: dict, reacted_at: datetime) -> None:
        """Append a reaction record. alert is the dict from SentAlertsRegistry."""
        meaning = EMOJI_MEANINGS.get(emoji, "unknown")
        record = {
            "message_id":  message_id,
            "emoji":       emoji,
            "meaning":     meaning,
            "reacted_at":  reacted_at.isoformat(),
            **alert,       # ticker, alert_type, price_str, price, signal, confidence, fired_at, source
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(
                f"Reaction logged: {emoji} ({meaning}) on {alert.get('ticker')} "
                f"{alert.get('alert_type')} {alert.get('price_str')}"
            )
        except OSError as e:
            logger.error(f"Could not write feedback log: {e}")

    def load_for_ticker(self, ticker: str) -> list[dict]:
        """Return all feedback records for a given ticker."""
        if not self._path.exists():
            return []
        records = []
        try:
            with self._path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("ticker") == ticker:
                            records.append(r)
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.error(f"Could not read feedback log: {e}")
        return records

    def load_all(self) -> list[dict]:
        """Return every feedback record."""
        if not self._path.exists():
            return []
        records = []
        try:
            with self._path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.error(f"Could not read feedback log: {e}")
        return records


# ---------------------------------------------------------------------------
# ConversationStore — user replies to bot messages
# ---------------------------------------------------------------------------

class ConversationStore:
    """
    Append-only log of user text replies to bot messages (data/conversations.jsonl).

    Each record links the user's message to the original bot message context
    (ticker, alert type, price level). Used by the analysis pipeline to
    understand the user's real-time thinking when `react: true` is set.
    """

    def __init__(self, path: Path):
        self._path = path

    def log_reply(
        self,
        user_message: str,
        original_message_id: int,
        alert_context: Optional[dict],
        ticker: Optional[str],
        replied_at: datetime,
    ) -> None:
        """Append a conversation record."""
        record = {
            "type": "reply",
            "user_message": user_message,
            "original_message_id": original_message_id,
            "ticker": ticker,
            "replied_at": replied_at.isoformat(),
        }
        # Merge alert context if we have it (ticker, alert_type, price_str, etc.)
        if alert_context:
            record["alert_context"] = alert_context
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(
                f"Conversation logged: reply to {ticker or 'unknown'} "
                f"msg#{original_message_id}: {user_message[:60]}..."
            )
        except OSError as e:
            logger.error(f"Could not write conversation log: {e}")

    def load_for_ticker(self, ticker: str) -> list[dict]:
        """Return all conversation records for a given ticker."""
        if not self._path.exists():
            return []
        records = []
        try:
            with self._path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("ticker") == ticker:
                            records.append(r)
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.error(f"Could not read conversation log: {e}")
        return records
