"""
Core alert logic: price crossing detection, cooldowns, calendar alerts.

Direction rules (from CLAUDE.md):
  BUY   — fires when price drops FROM ABOVE into/below the level
  SELL  — fires when price rises FROM BELOW into/above the level
  WATCH — fires in either direction

Startup behaviour:
  The first price poll only initialises prev_prices (warming-up phase).
  No alerts fire until the second poll, avoiding a burst on every restart.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pytz

from .parser import StockConfig, AlertLevel, CalendarAlert
from .state import BotState

logger = logging.getLogger(__name__)


@dataclass
class FiredAlert:
    """Structured record of a triggered alert (used for logging and the TUI)."""
    ticker: str
    alert_type: str   # BUY | SELL | WATCH
    price_str: str
    message: str
    signal: str       # original emoji from markdown
    confidence: int   # 1–5 for BUY, 1–4 for SELL
    source: str = "claude"   # "claude" | "manual"
    lower: float = 0.0   # lower bound of trigger range (from AlertLevel)
    upper: float = 0.0   # upper bound of trigger range (from AlertLevel)


class AlertEngine:
    def __init__(
        self,
        state: BotState,
        tz: pytz.BaseTzInfo,
        cooldown_minutes: int = 30,
        special_cooldown_days: int = 1,
    ):
        self._state = state
        self._tz = tz
        self._cooldown_min = cooldown_minutes
        self._special_cooldown_days = special_cooldown_days

        # ticker -> last known price (set on first poll, alerts fire from second poll)
        self._prev: dict[str, float] = {}
        self._warmed_up: set[str] = set()   # tickers that have had their first poll

    # ------------------------------------------------------------------
    # Price-level alerts
    # ------------------------------------------------------------------

    # Default hysteresis: price must clear the trigger point by this fraction
    # of ATR before a level can re-fire.  0.15 × ATR is tight enough to catch
    # real re-entries while filtering boundary noise.  (Was 0.25 but that
    # created dead zones too wide for volatile small/mid-cap stocks, causing
    # levels to stay silent for hours.  Combined with the 4-hour auto re-arm
    # in BotState.is_level_disarmed(), 0.15 is the right balance.)
    HYSTERESIS_ATR_FRACTION = 0.15

    def check_prices(
        self, stocks: list[StockConfig], prices: dict[str, float],
        atrs: dict[str, float] | None = None,
    ) -> list[FiredAlert]:
        """
        Compare new prices against alert levels.
        Returns list of FiredAlert (may be empty).

        If *atrs* is provided, hysteresis is applied: after a level fires it
        is "disarmed" and won't re-fire until the price moves away from the
        trigger point by 0.25 × ATR.  This prevents rapid-fire alerts when
        the price oscillates around a zone boundary.
        """
        alerts = []
        if atrs is None:
            atrs = {}

        for stock in stocks:
            ticker = stock.ticker
            curr = prices.get(ticker)
            if curr is None:
                continue

            prev = self._prev.get(ticker)

            if prev is None or ticker not in self._warmed_up:
                # First time we see this ticker — record price, do not fire
                self._prev[ticker] = curr
                self._warmed_up.add(ticker)
                logger.debug(f"{ticker} warming up at ₹{curr:.2f}")
                continue

            atr = atrs.get(ticker, 0.0)
            hysteresis = atr * self.HYSTERESIS_ATR_FRACTION

            for level in stock.levels:
                key = BotState.level_key(ticker, level.price_str)

                # --- Hysteresis: check if a disarmed level should re-arm ---
                if hysteresis > 0 and self._state.is_level_disarmed(key):
                    # Determine the trigger bound for this level type
                    trigger = level.upper if level.alert_type == "BUY" else level.lower
                    self._state.try_rearm_level(
                        key, level.alert_type, trigger, curr, hysteresis,
                    )
                    # If still disarmed after the check, skip this level
                    if self._state.is_level_disarmed(key):
                        continue

                if self._crosses(level, prev, curr):
                    if self._state.level_cooled_down(key, self._cooldown_min):
                        logger.info(f"ALERT {ticker} {level.alert_type} {level.price_str}")
                        alerts.append(FiredAlert(
                            ticker=ticker,
                            alert_type=level.alert_type,
                            price_str=level.price_str,
                            message=level.message,
                            signal=level.signal,
                            confidence=level.confidence,
                            lower=level.lower,
                            upper=level.upper,
                        ))
                        self._state.mark_level_fired(key, price=curr)

            self._prev[ticker] = curr

        return alerts

    # ------------------------------------------------------------------
    # Custom (manual) alerts
    # ------------------------------------------------------------------

    def check_custom_alerts(
        self, custom_store, prices: dict[str, float]
    ) -> list[FiredAlert]:
        """
        Check user-defined custom alerts against current prices.
        Uses the same crossing/cooldown logic as check_prices().
        Only fires for tickers whose prices have already been fetched.
        Cooldown keys are prefixed with "custom:" to avoid clashing with
        Claude alert keys.
        """
        alerts = []

        for ticker in custom_store.all_tickers():
            curr = prices.get(ticker)
            if curr is None:
                continue

            prev = self._prev.get(ticker)
            if prev is None or ticker not in self._warmed_up:
                continue  # not warmed up yet for this ticker

            for level in custom_store.to_alert_levels(ticker):
                if self._crosses(level, prev, curr):
                    key = BotState.level_key(f"custom:{ticker}", level.price_str)
                    if self._state.level_cooled_down(key, self._cooldown_min):
                        logger.info(f"MANUAL ALERT {ticker} {level.alert_type} {level.price_str}")
                        alerts.append(FiredAlert(
                            ticker=ticker,
                            alert_type=level.alert_type,
                            price_str=level.price_str,
                            message=level.message,
                            signal=level.signal,
                            confidence=level.confidence,
                            source="manual",
                            lower=level.lower,
                            upper=level.upper,
                        ))
                        self._state.mark_level_fired(key)

        return alerts

    # ------------------------------------------------------------------
    # Calendar alerts
    # ------------------------------------------------------------------

    def check_calendar_alerts(self, stocks: list[StockConfig]) -> list[str]:
        """
        Check date-based special alerts. Call once per trading day at market open.
        Returns list of alert messages to send.
        """
        now = datetime.now(self._tz)
        alerts = []

        for stock in stocks:
            for ca in stock.calendar_alerts:
                if now.month == ca.month and now.year == ca.year:
                    key = BotState.calendar_key(stock.ticker, ca.month, ca.year)
                    if self._state.calendar_cooled_down(key, self._special_cooldown_days):
                        logger.info(f"CALENDAR ALERT {stock.ticker} {ca.month}/{ca.year}")
                        alerts.append(ca.message)
                        self._state.mark_calendar_fired(key)

        return alerts

    # ------------------------------------------------------------------
    # Forecast-based "zone approaching" alerts
    # ------------------------------------------------------------------

    def check_forecast_alerts(
        self, stocks: list[StockConfig], prices: dict[str, float],
        forecasts: dict[str, dict],
    ) -> list[str]:
        """
        Check Prophet forecasts against alert levels. If the 30-day forecast
        CI includes a BUY or SELL level that the price hasn't reached yet,
        fire a "zone approaching" alert.

        Args:
            stocks: active stock configs
            prices: current prices
            forecasts: {ticker: {30: {lower, upper, predicted, trend}, ...}}

        Returns list of formatted alert messages.
        Daily cooldown per zone (not 30-min).
        """
        alerts = []
        for stock in stocks:
            ticker = stock.ticker
            curr = prices.get(ticker)
            fc = forecasts.get(ticker)
            if curr is None or fc is None:
                continue

            fc_30 = fc.get(30)
            if fc_30 is None:
                continue

            for level in stock.levels:
                # Only fire for zones the price hasn't already entered
                if level.alert_type == "BUY" and curr <= level.upper:
                    continue  # already in or below the zone
                if level.alert_type == "SELL" and curr >= level.lower:
                    continue  # already in or above the zone

                # Check if the 30-day forecast CI includes this level
                in_range = False
                if level.alert_type == "BUY":
                    # Price would need to DROP into this zone
                    in_range = fc_30["lower"] <= level.upper
                elif level.alert_type == "SELL":
                    # Price would need to RISE into this zone
                    in_range = fc_30["upper"] >= level.lower

                if not in_range:
                    continue

                # Daily cooldown per forecast zone
                key = f"forecast:{ticker}:{level.price_str}"
                # Use calendar cooldown (1 day) since these fire at most daily
                if self._state.calendar_cooled_down(key, self._special_cooldown_days):
                    direction = "tested" if level.alert_type == "BUY" else "reached"
                    msg = (
                        f"📊  FORECAST  {ticker} {level.price_str} "
                        f"{level.alert_type} zone likely to be {direction} "
                        f"within 30 days. Prepare."
                    )
                    alerts.append(msg)
                    self._state.mark_calendar_fired(key)
                    logger.info(f"FORECAST ALERT {ticker} {level.alert_type} {level.price_str}")

        return alerts

    # ------------------------------------------------------------------
    # Approach alerts — "getting close to a level" for quiet stocks
    # ------------------------------------------------------------------

    def check_approach_alerts(
        self,
        stocks: list[StockConfig],
        prices: dict[str, float],
        atrs: dict[str, float],
        recent_alert_counts: dict[str, int],
        dead_zone_threshold_pct: float = 12.0,
        max_recent_alerts: int = 5,
        atr_multiplier: float = 1.0,
        cooldown_hours: int = 24,
    ) -> list[str]:
        """
        Fire a nudge when price drifts within 1×ATR of a BUY or SELL level,
        but only for stocks that are "quiet" — wide dead zone AND few recent alerts.

        Auto-calibrated per stock: stocks that already fire enough alerts
        (tight dead zone or volatile) are skipped entirely.

        Returns list of formatted message strings (sent via notifier.send_many).
        Uses calendar_cooled_down with cooldown_hours for per-level throttling.
        """
        alerts: list[str] = []

        for stock in stocks:
            ticker = stock.ticker
            curr = prices.get(ticker)
            atr = atrs.get(ticker)
            if curr is None or atr is None or atr <= 0:
                continue

            # --- Qualification gate: is this stock quiet enough to need nudges? ---

            # Separate BUY and SELL levels (skip WATCH/HOLD)
            buy_levels = [l for l in stock.levels if l.alert_type == "BUY"]
            sell_levels = [l for l in stock.levels if l.alert_type == "SELL"]

            # Find nearest BUY below price and nearest SELL above price
            buys_below = [l for l in buy_levels if l.upper < curr]
            sells_above = [l for l in sell_levels if l.lower > curr]

            if not buys_below and not sells_above:
                continue  # no levels to approach

            nearest_buy = max(buys_below, key=lambda l: l.upper) if buys_below else None
            nearest_sell = min(sells_above, key=lambda l: l.lower) if sells_above else None

            # Calculate dead zone as % of current price
            if nearest_buy and nearest_sell:
                dead_zone_pct = (nearest_sell.lower - nearest_buy.upper) / curr * 100
            elif nearest_buy:
                dead_zone_pct = 100.0  # no sell above → infinite dead zone upward
            else:
                dead_zone_pct = 100.0  # no buy below → infinite dead zone downward

            # Gate 1: dead zone must exceed threshold
            if dead_zone_pct < dead_zone_threshold_pct:
                continue

            # Gate 2: stock must have fired fewer than max_recent_alerts in last 30 days
            if recent_alert_counts.get(ticker, 0) >= max_recent_alerts:
                continue

            # --- Proximity checks ---

            approach_distance = atr * atr_multiplier

            # Check BUY approach: price is close to (but still above) the buy level
            if nearest_buy:
                gap = curr - nearest_buy.upper
                if 0 < gap <= approach_distance:
                    key = f"approach:{ticker}:{nearest_buy.price_str}"
                    # Convert hours to days for calendar_cooled_down (rounds up)
                    cooldown_days = max(1, cooldown_hours // 24)
                    if self._state.calendar_cooled_down(key, cooldown_days):
                        gap_pct = gap / curr * 100
                        msg = (
                            f"📡  APPROACH  {ticker} at ₹{curr:.0f} — "
                            f"{gap_pct:.1f}% from {nearest_buy.price_str} "
                            f"BUY zone. ATR ₹{atr:.0f}. Watch for entry signal."
                        )
                        alerts.append(msg)
                        self._state.mark_calendar_fired(key)
                        logger.info(f"APPROACH {ticker} → {nearest_buy.price_str} BUY ({gap_pct:.1f}% away)")

            # Check SELL approach: price is close to (but still below) the sell level
            if nearest_sell:
                gap = nearest_sell.lower - curr
                if 0 < gap <= approach_distance:
                    key = f"approach:{ticker}:{nearest_sell.price_str}"
                    cooldown_days = max(1, cooldown_hours // 24)
                    if self._state.calendar_cooled_down(key, cooldown_days):
                        gap_pct = gap / curr * 100
                        msg = (
                            f"📡  APPROACH  {ticker} at ₹{curr:.0f} — "
                            f"{gap_pct:.1f}% from {nearest_sell.price_str} "
                            f"SELL zone. ATR ₹{atr:.0f}. Prepare to trim."
                        )
                        alerts.append(msg)
                        self._state.mark_calendar_fired(key)
                        logger.info(f"APPROACH {ticker} → {nearest_sell.price_str} SELL ({gap_pct:.1f}% away)")

        return alerts

    # ------------------------------------------------------------------
    # Crossing logic
    # ------------------------------------------------------------------

    @staticmethod
    def _crosses(level: AlertLevel, prev: float, curr: float) -> bool:
        """
        True if the price moved from one side of the level to the other
        in a direction that matches the alert type.
        """
        if level.alert_type == "BUY":
            # Price dropped from above the upper bound into/below it
            return prev > level.upper and curr <= level.upper

        elif level.alert_type == "SELL":
            # Price rose from below the lower bound into/above it
            return prev < level.lower and curr >= level.lower

        elif level.alert_type == "WATCH":
            # Either direction into the band
            dropped_in = prev > level.upper and curr <= level.upper
            rose_in = prev < level.lower and curr >= level.lower
            return dropped_in or rose_in

        return False
