"""
Real-time price cache backed by Groww Trade API (NATS protocol).

GrowwPriceFeed.start() resolves NSE instrument tokens, seeds initial prices via REST,
and opens a Groww feed connection in a daemon thread.

get_price(yf_symbol) → cached price (< 60 s) → REST ltp() fallback → None.
Thread-safety: all cache writes go through _lock; _groww is also read under _lock.
"""
import logging
import threading
import time
from typing import Optional

from growwapi import GrowwAPI, GrowwFeed

logger = logging.getLogger(__name__)

_STALE_SECONDS = 60


def _to_trading_symbol(yf_symbol: str) -> str:
    """'STLTECH.NS' → 'STLTECH'"""
    return yf_symbol.removesuffix(".NS")


class GrowwPriceFeed:

    def __init__(self, groww: GrowwAPI, yf_symbols: list[str]) -> None:
        self._groww = groww
        self._yf_symbols = yf_symbols
        self._price_cache: dict[str, float] = {}      # yf_symbol → price
        self._last_tick_time: dict[str, float] = {}   # yf_symbol → unix ts
        self._token_to_symbol: dict[str, str] = {}    # str(exchange_token) → yf_symbol
        self._instrument_list: list[dict] = []         # current subscription descriptors
        self._groww_feed: Optional[GrowwFeed] = None
        self._consume_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Resolve tokens, seed prices, open feed. Call once at startup."""
        self._resolve_tokens()
        self._start_feed()

    def stop(self) -> None:
        """Cleanly close the feed connection."""
        with self._lock:
            feed = self._groww_feed
            instrument_list = list(self._instrument_list)
            self._groww_feed = None
        if feed is None:
            return
        try:
            if instrument_list:
                feed.unsubscribe_ltp(instrument_list)
        except Exception:
            pass
        try:
            nc = feed._nats_client
            if nc._loop.is_running():
                nc._loop.call_soon_threadsafe(nc._loop.stop)
        except Exception:
            pass
        if self._consume_thread is not None:
            self._consume_thread.join(timeout=2.0)
        self._consume_thread = None

    def refresh_subscriptions(self, yf_symbols: list[str]) -> None:
        """
        Re-subscribe after stock list changes or daily token renewal.
        Re-auth is handled internally; on failure the existing connection is kept.
        """
        from alert_bot.groww_auth import get_groww_client
        try:
            new_groww = get_groww_client()
            if new_groww is not None:
                with self._lock:
                    self._groww = new_groww
        except Exception as exc:
            logger.warning(
                "Groww re-auth failed during refresh; keeping existing connection: %s", exc
            )
        self._yf_symbols = yf_symbols
        self._resolve_tokens()
        self._start_feed()

    def get_price(self, yf_symbol: str) -> Optional[float]:
        """Return latest price, or None if unavailable (caller falls back to yfinance)."""
        with self._lock:
            price     = self._price_cache.get(yf_symbol)
            last_tick = self._last_tick_time.get(yf_symbol, 0.0)
            groww     = self._groww

        if price is not None and (time.time() - last_tick) < _STALE_SECONDS:
            return price

        trading_symbol = _to_trading_symbol(yf_symbol)
        try:
            resp = groww.get_ltp((trading_symbol,), segment="CASH")
            logger.debug("Groww REST ltp for %s: %s", trading_symbol, resp)
            ltp = resp.get(trading_symbol, {}).get("ltp")
            if ltp is not None:
                return float(ltp)
        except Exception as exc:
            logger.warning("Groww REST ltp failed for %s: %s", trading_symbol, exc)
        return None

    def _resolve_tokens(self) -> None:
        """Look up exchange_token for each symbol; build instrument list."""
        new_token_map: dict[str, str] = {}
        new_instrument_list: list[dict] = []

        for yf_symbol in self._yf_symbols:
            ticker = _to_trading_symbol(yf_symbol)
            try:
                instrument = self._groww.get_instrument_by_exchange_and_trading_symbol(
                    exchange="NSE",
                    trading_symbol=ticker,
                )
                token = str(instrument["exchange_token"])  # normalise to str
                new_token_map[token] = yf_symbol
                new_instrument_list.append({
                    "exchange": "NSE",
                    "segment": "CASH",
                    "exchange_token": token,
                })
            except Exception as exc:
                logger.error("Failed to resolve Groww token for %s: %s", ticker, exc)

        missing = set(self._yf_symbols) - set(new_token_map.values())
        if missing:
            logger.warning(
                "Groww token resolution: %d symbol(s) not found: %s",
                len(missing), sorted(missing),
            )

        with self._lock:
            self._token_to_symbol   = new_token_map
            self._instrument_list   = new_instrument_list

    def _start_feed(self) -> None:
        """Stop existing feed, open a new GrowwFeed, and start the consume daemon thread."""
        self.stop()

        with self._lock:
            instrument_list = list(self._instrument_list)
            groww            = self._groww

        if not instrument_list:
            logger.warning("No Groww instrument tokens resolved; feed not started")
            return

        self._groww_feed = GrowwFeed(groww)

        def on_data_received(meta: dict) -> None:
            # meta carries routing keys only — the actual price lives in get_ltp()
            token    = str(meta.get("feed_key", ""))
            exchange = meta.get("exchange", "NSE")
            segment  = meta.get("segment", "CASH")

            with self._lock:
                yf_symbol = self._token_to_symbol.get(token)
            if yf_symbol is None:
                return

            try:
                active_feed = self._groww_feed  # snapshot before concurrent stop() can null it
                if active_feed is None:
                    return
                snapshot   = active_feed.get_ltp()
                price_data = snapshot.get(exchange, {}).get(segment, {}).get(token)
                if price_data is None:
                    return
                price = price_data.get("ltp")
                if price is not None:
                    with self._lock:
                        self._price_cache[yf_symbol]    = float(price)
                        self._last_tick_time[yf_symbol] = time.time()
            except Exception as exc:
                logger.debug("Groww tick processing error: %s", exc)

        self._groww_feed.subscribe_ltp(instrument_list, on_data_received=on_data_received)

        # consume() blocks the NATS event loop — run it in a daemon thread so
        # the main process can exit cleanly without waiting for it.
        self._consume_thread = threading.Thread(
            target=self._groww_feed.consume,
            daemon=True,
            name="groww-feed-consume",
        )
        self._consume_thread.start()
        logger.info("Groww feed started; subscribed %d symbols", len(instrument_list))
