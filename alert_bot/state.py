"""
Persists bot state (cooldowns, direction tracking) to disk so restarts
don't cause alert bursts or miss the 30-minute cooldown window.
"""
import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

_DATE_FMT = "%Y-%m-%d"
_DT_FMT = "%Y-%m-%dT%H:%M:%S%z"


class BotState:
    """
    Holds and persists:
      - level_cooldowns:       {level_key -> datetime} last time this level fired
      - calendar_cooldowns:    {calendar_key -> date}   last date a calendar alert fired
      - mmi_last_value:        float   last MMI value that triggered an alert
      - mmi_last_zone:         str     last MMI zone that triggered an alert
      - mmi_last_alert_dt:     datetime last time an MMI alert was sent
      - mmi_pin_mode:          "full" | "compact"
      - mmi_pinned_message_id: int | None  message_id of the compact pinned message
    """

    def __init__(self, state_file: Path, tz: pytz.BaseTzInfo):
        self._file = state_file
        self._tz = tz
        self.level_cooldowns: dict[str, datetime] = {}
        self.calendar_cooldowns: dict[str, date] = {}
        # Hysteresis: after a level fires, it's "disarmed" until price clears
        # the trigger point by 0.25×ATR.  Maps level_key → price when it fired.
        self.disarmed_levels: dict[str, float] = {}
        self.mmi_last_value: Optional[float] = None
        self.mmi_last_zone: Optional[str] = None
        self.mmi_last_alert_dt: Optional[datetime] = None
        self.mmi_pin_mode: str = "full"
        self.mmi_pinned_message_id: Optional[int] = None
        self.alert_mode: str = "both"   # "claude" | "manual" | "both"
        # Dalal Street morning digest: list of date strings already processed
        self.digest_processed: list[str] = []
        # Gold tracker: last observed 24K ₹/gram (for zone-crossing detection
        # across bot restarts) and last classified regime (for transition
        # detection if 30D history is unavailable on a fresh cache).
        self.gold_last_inr_per_gram: Optional[float] = None
        self.gold_last_regime: Optional[str] = None
        self.gold_last_check_date: Optional[date] = None
        # Stock regime tracking: {ticker: regime_name} for transition detection
        # across restarts.  Updated once daily at market open by HMM scan.
        self.stock_regimes: dict[str, str] = {}
        self.regime_scan_date: Optional[date] = None
        # Regime probability history: {ticker: [prob, prob, ...]} last 5 readings.
        # Used to compute direction arrow (↑/→/↓) in alert headers.
        self.regime_prob_history: dict[str, list[float]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @staticmethod
    def level_key(ticker: str, price_str: str) -> str:
        return f"{ticker}:{price_str}"

    @staticmethod
    def calendar_key(ticker: str, month: int, year: int) -> str:
        return f"{ticker}:{month}:{year}"

    def level_cooled_down(self, key: str, cooldown_minutes: int) -> bool:
        """True if enough time has passed since the level last fired."""
        last = self.level_cooldowns.get(key)
        if last is None:
            return True
        now = datetime.now(self._tz)
        return (now - last).total_seconds() >= cooldown_minutes * 60

    # After this many hours, a disarmed level automatically re-arms even if
    # price hasn't cleared the hysteresis band.  This prevents levels from
    # going permanently silent when price oscillates near a zone boundary.
    DISARM_EXPIRY_HOURS = 4

    def mark_level_fired(self, key: str, price: float = 0.0) -> None:
        self.level_cooldowns[key] = datetime.now(self._tz)
        # Disarm this level until price clears the trigger by the hysteresis band
        # or DISARM_EXPIRY_HOURS have passed (whichever comes first).
        if price > 0:
            self.disarmed_levels[key] = {
                "price": price,
                "disarmed_at": datetime.now(self._tz).strftime(_DT_FMT),
            }

    def is_level_disarmed(self, key: str) -> bool:
        """True if this level has fired and hasn't been re-armed yet.

        Automatically re-arms levels that have been disarmed for longer than
        DISARM_EXPIRY_HOURS — this is the safety net that prevents a level
        from going permanently silent when price hovers near the boundary.
        """
        entry = self.disarmed_levels.get(key)
        if entry is None:
            return False

        # Time-based auto re-arm: if the level has been disarmed for too long,
        # re-arm it so it can fire again on the next crossing.
        disarmed_at_str = entry.get("disarmed_at") if isinstance(entry, dict) else None
        if disarmed_at_str:
            try:
                disarmed_at = datetime.strptime(disarmed_at_str, _DT_FMT).astimezone(self._tz)
                hours_elapsed = (datetime.now(self._tz) - disarmed_at).total_seconds() / 3600
                if hours_elapsed >= self.DISARM_EXPIRY_HOURS:
                    del self.disarmed_levels[key]
                    logger.debug(f"Auto re-armed {key} after {hours_elapsed:.1f}h disarmed")
                    return False
            except (ValueError, TypeError):
                pass
        else:
            # Legacy format (bare float) — no timestamp, so re-arm immediately.
            # This cleans up old state from before the fix.
            del self.disarmed_levels[key]
            logger.debug(f"Re-armed {key} (legacy format, no timestamp)")
            return False

        return True

    def try_rearm_level(
        self, key: str, alert_type: str, trigger_bound: float,
        curr_price: float, hysteresis: float,
    ) -> bool:
        """
        Check if price has cleared the trigger point by enough to re-arm.
        Returns True if the level was just re-armed (or was never disarmed).

        For BUY levels (trigger = upper bound): price must rise above
        trigger + hysteresis before the level can fire again on re-entry.

        For SELL levels (trigger = lower bound): price must drop below
        trigger - hysteresis before the level can fire again on re-entry.
        """
        if key not in self.disarmed_levels:
            return True  # already armed

        if alert_type == "BUY":
            # Price dropped into zone — needs to climb back above trigger + band
            if curr_price > trigger_bound + hysteresis:
                del self.disarmed_levels[key]
                logger.debug(f"Re-armed {key} (price ₹{curr_price:.1f} > ₹{trigger_bound + hysteresis:.1f})")
                return True
        elif alert_type == "SELL":
            # Price rose into zone — needs to drop back below trigger - band
            if curr_price < trigger_bound - hysteresis:
                del self.disarmed_levels[key]
                logger.debug(f"Re-armed {key} (price ₹{curr_price:.1f} < ₹{trigger_bound - hysteresis:.1f})")
                return True
        elif alert_type == "WATCH":
            # WATCH fires both directions — re-arm when price clears either side
            entry = self.disarmed_levels[key]
            # Handle both new dict format {"price": ..., "disarmed_at": ...}
            # and legacy bare-float format
            fired_price = entry["price"] if isinstance(entry, dict) else entry
            if curr_price > fired_price:
                # Moved up from where it fired — need to clear upward
                if curr_price > trigger_bound + hysteresis:
                    del self.disarmed_levels[key]
                    return True
            else:
                # Moved down — need to clear downward
                if curr_price < trigger_bound - hysteresis:
                    del self.disarmed_levels[key]
                    return True

        return False  # still disarmed

    def calendar_cooled_down(self, key: str, cooldown_days: int) -> bool:
        """True if we haven't fired this calendar alert within cooldown_days."""
        last = self.calendar_cooldowns.get(key)
        if last is None:
            return True
        today = datetime.now(self._tz).date()
        return (today - last).days >= cooldown_days

    def mark_calendar_fired(self, key: str) -> None:
        self.calendar_cooldowns[key] = datetime.now(self._tz).date()

    def should_alert_mmi(self, new_value: float, new_zone: str) -> bool:
        """True if the MMI has crossed into a new zone."""
        if self.mmi_last_zone is None:
            return False  # don't alert on startup, just set baseline
        return new_zone != self.mmi_last_zone

    def mark_mmi_alerted(self, value: float, zone: str) -> None:
        self.mmi_last_value = value
        self.mmi_last_zone = zone
        self.mmi_last_alert_dt = datetime.now(self._tz)

    def is_digest_processed(self, date_str: str) -> bool:
        """True if this date's digest file has already been sent."""
        return date_str in self.digest_processed

    def mark_digest_processed(self, date_str: str) -> None:
        if date_str not in self.digest_processed:
            self.digest_processed.append(date_str)

    def update_mmi_baseline(self, value: float, zone: str) -> None:
        """Set baseline without marking as alerted (used on startup)."""
        self.mmi_last_value = value
        self.mmi_last_zone = zone

    def push_regime_prob(self, ticker: str, prob: float) -> None:
        """Append a regime confidence reading; keep last 5."""
        hist = self.regime_prob_history.setdefault(ticker, [])
        hist.append(round(prob, 4))
        if len(hist) > 5:
            hist[:] = hist[-5:]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "level_cooldowns": {
                k: v.strftime(_DT_FMT) for k, v in self.level_cooldowns.items()
            },
            "calendar_cooldowns": {
                k: v.strftime(_DATE_FMT) for k, v in self.calendar_cooldowns.items()
            },
            "disarmed_levels": self.disarmed_levels,
            "alert_mode": self.alert_mode,
            "digest_processed": self.digest_processed,
            "gold": {
                "last_inr_per_gram": self.gold_last_inr_per_gram,
                "last_regime": self.gold_last_regime,
                "last_check_date": self.gold_last_check_date.strftime(_DATE_FMT) if self.gold_last_check_date else None,
            },
            "mmi": {
                "last_value": self.mmi_last_value,
                "last_zone": self.mmi_last_zone,
                "last_alert_dt": self.mmi_last_alert_dt.strftime(_DT_FMT) if self.mmi_last_alert_dt else None,
                "pin_mode": self.mmi_pin_mode,
                "pinned_message_id": self.mmi_pinned_message_id,
            },
            "stock_regimes": {
                "regimes": self.stock_regimes,
                "scan_date": self.regime_scan_date.strftime(_DATE_FMT) if self.regime_scan_date else None,
            },
            "regime_prob_history": self.regime_prob_history,
        }
        try:
            self._file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            logger.error(f"Could not save state: {e}")

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            for k, v in data.get("level_cooldowns", {}).items():
                self.level_cooldowns[k] = datetime.strptime(v, _DT_FMT).astimezone(self._tz)
            for k, v in data.get("calendar_cooldowns", {}).items():
                self.calendar_cooldowns[k] = datetime.strptime(v, _DATE_FMT).date()
            # Load disarmed levels — supports both new dict format
            # {"price": float, "disarmed_at": str} and legacy bare-float format
            raw_disarmed = data.get("disarmed_levels", {})
            self.disarmed_levels = {}
            for k, v in raw_disarmed.items():
                if isinstance(v, dict):
                    self.disarmed_levels[k] = v  # new format, keep as-is
                else:
                    self.disarmed_levels[k] = float(v)  # legacy bare float
            mmi = data.get("mmi", {})
            self.mmi_last_value        = mmi.get("last_value")
            self.mmi_last_zone         = mmi.get("last_zone")
            self.mmi_pin_mode          = mmi.get("pin_mode", "full")
            self.mmi_pinned_message_id = mmi.get("pinned_message_id")
            self.alert_mode            = data.get("alert_mode", "both")
            self.digest_processed      = data.get("digest_processed", [])
            gold = data.get("gold", {})
            self.gold_last_inr_per_gram = gold.get("last_inr_per_gram")
            self.gold_last_regime = gold.get("last_regime")
            if gold.get("last_check_date"):
                try:
                    self.gold_last_check_date = datetime.strptime(
                        gold["last_check_date"], _DATE_FMT
                    ).date()
                except ValueError:
                    self.gold_last_check_date = None
            if mmi.get("last_alert_dt"):
                self.mmi_last_alert_dt = datetime.strptime(mmi["last_alert_dt"], _DT_FMT).astimezone(self._tz)
            # Stock regimes (HMM): {ticker: regime_name} for transition detection
            sr = data.get("stock_regimes", {})
            self.stock_regimes = sr.get("regimes", {})
            self.regime_prob_history = data.get("regime_prob_history", {})
            if sr.get("scan_date"):
                try:
                    self.regime_scan_date = datetime.strptime(
                        sr["scan_date"], _DATE_FMT
                    ).date()
                except ValueError:
                    self.regime_scan_date = None
            logger.info(f"Loaded state: {len(self.level_cooldowns)} level cooldowns, "
                        f"{len(self.calendar_cooldowns)} calendar cooldowns")
        except Exception as e:
            logger.warning(f"Could not load state (starting fresh): {e}")
