"""
Telegram notification sender.
Uses the raw Bot API (no extra library needed beyond `requests`).
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

_SEND_DELAY = 0.5   # seconds between messages when sending multiple alerts at once


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._base = f"https://api.telegram.org/bot{token}"
        self._url = f"{self._base}/sendMessage"
        self._chat_id = chat_id
        self._token = token
        self._connection_ok = True

    def send(self, message: str) -> int | None:
        """Send a message. Returns the message_id on success, None on failure."""
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["result"]["message_id"]
        except requests.RequestException as e:
            logger.error(f"Telegram send failed: {e}")
            return None

    def pin_message(self, message_id: int) -> None:
        """Pin a message in the chat."""
        try:
            resp = requests.post(
                f"{self._base}/pinChatMessage",
                json={"chat_id": self._chat_id, "message_id": message_id},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Telegram pin failed: {e}")

    def edit_message(
        self,
        message_id: int,
        text: str,
        chat_id: int | str | None = None,
        reply_markup: dict | None = None,
    ) -> bool:
        """Edit an existing message in place. Returns True on success.
        Pass reply_markup={"inline_keyboard": []} to strip existing buttons."""
        try:
            payload = {
                "chat_id": chat_id if chat_id is not None else self._chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            resp = requests.post(
                f"{self._base}/editMessageText",
                json=payload,
                timeout=10,
            )
            # Telegram returns 400 when text is identical — content is already correct
            if resp.status_code == 400 and "message is not modified" in resp.text:
                return True
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Telegram edit failed: {e}")
            return False

    def reply(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> int | None:
        """Send a reply to a specific chat (used by command listener).
        If reply_to_message_id is set, the message threads under that message.
        reply_markup attaches an inline keyboard (e.g. for confirm/cancel buttons)."""
        try:
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            resp = requests.post(self._url, json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json()["result"]["message_id"]
        except requests.RequestException as e:
            logger.error(f"Telegram reply failed: {e}")
            return None

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge an inline-keyboard button press. Telegram requires this
        within ~15s or the button shows a perpetual loading spinner."""
        try:
            payload = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            resp = requests.post(
                f"{self._base}/answerCallbackQuery", json=payload, timeout=10
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Telegram answerCallbackQuery failed: {e}")

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        """Long-poll for new updates. Returns list of update dicts."""
        try:
            resp = requests.get(
                f"{self._base}/getUpdates",
                params={"offset": offset, "timeout": timeout,
                        "allowed_updates": ["message", "message_reaction", "callback_query"]},
                timeout=timeout + 5,
            )
            resp.raise_for_status()
            if not self._connection_ok:
                self._connection_ok = True
                logger.info("Telegram connection re-established")
                self.send("✅ Bot reconnected to Telegram.")
            return resp.json().get("result", [])
        except requests.RequestException as e:
            logger.error(f"Telegram getUpdates failed: {e}")
            self._connection_ok = False
            time.sleep(5)
            return []

    def send_document(self, file_path, caption: str = "") -> bool:
        """Send a file (e.g. PDF) to the chat. Returns True on success."""
        from pathlib import Path
        path = Path(file_path)
        try:
            with path.open("rb") as f:
                resp = requests.post(
                    f"{self._base}/sendDocument",
                    data={"chat_id": self._chat_id, "caption": caption},
                    files={"document": (path.name, f, "application/pdf")},
                    timeout=60,
                )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Telegram sendDocument failed: {e}")
            return False

    def send_many(self, messages: list[str]) -> None:
        """Send a list of messages with a small delay between each."""
        for i, msg in enumerate(messages):
            self.send(msg)
            if i < len(messages) - 1:
                time.sleep(_SEND_DELAY)

    def test_connection(self) -> bool:
        """Send a startup ping. Returns True on success."""
        ok = self.send("Stock alert bot is now running.")
        if ok:
            logger.info("Telegram connection confirmed.")
        else:
            logger.error("Could not reach Telegram — check your token and chat ID.")
        return ok
