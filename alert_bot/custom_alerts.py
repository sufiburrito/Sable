"""
Custom user-defined price alerts, added via the Telegram /alert command.
Persisted to data/custom_alerts.json.
"""
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from .parser import AlertLevel

logger = logging.getLogger(__name__)

# Confidence level (1-5) → display emoji, keyed by alert type
_SIGNAL: dict[str, dict[int, str]] = {
    "BUY":   {1: "🟡", 2: "🟢", 3: "🔵", 4: "🟠", 5: "🔴"},
    "SELL":  {1: "⬆️", 2: "⬆️⬆️", 3: "🚀", 4: "🚀🚀", 5: "🚀🚀"},
    "WATCH": {i: "👁️" for i in range(1, 6)},
}


@dataclass
class CustomAlertEntry:
    price_str: str   # e.g. "₹145"
    lower: float     # lower trigger bound
    upper: float     # upper trigger bound (= lower for single prices)
    alert_type: str  # BUY | SELL | WATCH
    confidence: int  # 1–5
    note: str        # optional note; empty string if none


class CustomAlertsStore:
    """Loads, saves and manages custom user alerts per ticker."""

    def __init__(self, store_file: Path):
        self._file = store_file
        self._data: dict[str, list[CustomAlertEntry]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def add(self, ticker: str, entries: list[CustomAlertEntry]) -> None:
        if ticker not in self._data:
            self._data[ticker] = []
        self._data[ticker].extend(entries)
        self._save()

    def list_alerts(self, ticker: str) -> list[CustomAlertEntry]:
        return list(self._data.get(ticker, []))

    def clear(self, ticker: str) -> int:
        """Backup then remove all custom alerts for ticker. Returns count removed."""
        count = len(self._data.get(ticker, []))
        if count:
            self.backup()
        self._data.pop(ticker, None)
        self._save()
        return count

    def clear_all(self) -> tuple[int, int]:
        """Backup then clear every manual alert. Returns (alert_count, ticker_count)."""
        alert_count = sum(len(v) for v in self._data.values())
        ticker_count = len(self._data)
        self.backup()
        self._data = {}
        self._save()
        return alert_count, ticker_count

    def backup(self) -> None:
        """Snapshot current state to backup file (overwrites previous backup)."""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        serialisable = {
            ticker: [asdict(e) for e in entries]
            for ticker, entries in self._data.items()
        }
        try:
            self._backup_file.write_text(
                json.dumps(serialisable, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"Could not write alert backup: {e}")

    def restore_backup(self) -> tuple[int, int] | None:
        """Restore from backup file. Returns (alert_count, ticker_count) or None."""
        if not self._backup_file.exists():
            return None
        try:
            raw = json.loads(self._backup_file.read_text(encoding="utf-8"))
            self._data = {
                ticker: [CustomAlertEntry(**e) for e in entries]
                for ticker, entries in raw.items()
            }
            self._save()
            alert_count = sum(len(v) for v in self._data.values())
            return alert_count, len(self._data)
        except Exception as e:
            logger.error(f"Could not restore alert backup: {e}")
            return None

    @property
    def _backup_file(self) -> Path:
        return self._file.parent / (self._file.stem + "_backup" + self._file.suffix)

    def all_tickers(self) -> list[str]:
        return list(self._data.keys())

    def to_alert_levels(self, ticker: str) -> list[AlertLevel]:
        """Convert stored entries into AlertLevel objects the engine can consume."""
        levels = []
        for e in self._data.get(ticker, []):
            signal = _SIGNAL.get(e.alert_type, {}).get(min(e.confidence, 5), "🔵")
            if e.note:
                message = f"{ticker} at {e.price_str} — {e.alert_type}. {e.note}"
            else:
                message = f"{ticker} at {e.price_str} — {e.alert_type}."
            levels.append(AlertLevel(
                signal=signal,
                price_str=e.price_str,
                lower=e.lower,
                upper=e.upper,
                alert_type=e.alert_type,
                message=message,
                confidence=e.confidence,
            ))
        return levels

    # ------------------------------------------------------------------ #
    # Telegram input parser                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_entries(ticker: str, raw: str) -> tuple[list[CustomAlertEntry], list[str]]:
        """
        Parse the comma-separated entries portion of an /alert command.

        Format per entry:  price TYPE [confidence] [note...]
        Examples:
          "145 BUY 3 Positive earnings call"
          "135 BUY Strong promoter floor"
          "148 SELL"

        Returns (valid_entries, error_messages).
        """
        entries: list[CustomAlertEntry] = []
        errors: list[str] = []

        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue

            tokens = part.split()
            if len(tokens) < 2:
                errors.append(f"Too few tokens: <code>{part}</code>")
                continue

            # Price
            try:
                price_raw = tokens[0].replace("₹", "").replace(",", "")
                price = float(price_raw)
            except ValueError:
                errors.append(f"Bad price: <code>{tokens[0]}</code>")
                continue

            # Alert type
            atype = tokens[1].upper()
            if atype not in ("BUY", "SELL", "WATCH"):
                errors.append(f"Unknown type <code>{tokens[1]}</code> — use BUY, SELL or WATCH")
                continue

            # Optional confidence (int 1-5)
            confidence = 3
            note_start = 2
            if len(tokens) > 2:
                try:
                    c = int(tokens[2])
                    if 1 <= c <= 5:
                        confidence = c
                        note_start = 3
                    # If out of range, treat as start of note
                except ValueError:
                    pass  # not an int → tokens[2] is start of note

            note = " ".join(tokens[note_start:])

            price_str = f"₹{price:g}"
            entries.append(CustomAlertEntry(
                price_str=price_str,
                lower=price,
                upper=price,
                alert_type=atype,
                confidence=confidence,
                note=note,
            ))

        return entries, errors

    # ------------------------------------------------------------------ #
    # Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        serialisable = {
            ticker: [asdict(e) for e in entries]
            for ticker, entries in self._data.items()
        }
        try:
            self._file.write_text(json.dumps(serialisable, indent=2), encoding="utf-8")
        except OSError as e:
            logger.error(f"Could not save custom alerts: {e}")

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            for ticker, entries in raw.items():
                self._data[ticker] = [CustomAlertEntry(**e) for e in entries]
            total = sum(len(v) for v in self._data.values())
            logger.info(f"Loaded {total} custom alerts across {len(self._data)} tickers")
        except Exception as e:
            logger.warning(f"Could not load custom alerts (starting fresh): {e}")
