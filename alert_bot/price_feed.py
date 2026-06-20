"""
PriceFeed Protocol and provider factory.

main.py interacts exclusively with this module — never with kite_feed or groww_feed
directly. The factory reads credentials from config and returns the appropriate
implementation, or None for yfinance-only mode.
"""
import logging
from typing import Optional, Protocol, runtime_checkable

from alert_bot.config import (
    GROWW_API_KEY,
    GROWW_TOTP_SECRET,
    ZERODHA_API_KEY,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class PriceFeed(Protocol):
    """
    Structural interface satisfied by KitePriceFeed and GrowwPriceFeed.
    Any class with these four methods qualifies — no inheritance required.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def get_price(self, yf_symbol: str) -> Optional[float]: ...
    def refresh_subscriptions(self, yf_symbols: list[str]) -> None: ...


def create_price_feed(yf_symbols: list[str]) -> Optional[PriceFeed]:
    """
    Return an initialised PriceFeed based on .env credentials, or None.

    Priority: Kite > Groww > None (yfinance-only).
    If both credential sets are present, Kite is used and a warning is logged.

    Imports are lazy (inside the function body) for two reasons:
    1. Avoids circular imports at module load time.
    2. Avoids loading growwapi/kiteconnect SDK unless those credentials are set.

    Patching note for tests: patch the *source* module paths
    (alert_bot.kite_auth.get_kite_client, alert_bot.kite_feed.KitePriceFeed, etc.)
    rather than alert_bot.price_feed.* — lazy imports don't bind names at module
    level, so patching on price_feed won't intercept them.
    """
    has_kite = bool(ZERODHA_API_KEY)
    has_groww = bool(GROWW_API_KEY and GROWW_TOTP_SECRET)

    if has_kite:
        if has_groww:
            logger.warning(
                "Both Kite and Groww credentials are set — using Kite. "
                "Unset ZERODHA_API_KEY to switch to Groww."
            )
        # Lazy import: only load kite modules when Kite credentials exist.
        from alert_bot.kite_auth import get_kite_client
        from alert_bot.kite_feed import KitePriceFeed

        kite = get_kite_client()
        if kite is not None:
            return KitePriceFeed(kite, yf_symbols)
        if has_groww:
            logger.info("Kite auth returned None — falling through to Groww")

    if has_groww:
        # Lazy import: only load groww modules when Groww credentials exist.
        from alert_bot.groww_auth import get_groww_client
        from alert_bot.groww_feed import GrowwPriceFeed

        groww = get_groww_client()
        if groww is not None:
            return GrowwPriceFeed(groww, yf_symbols)

    return None
