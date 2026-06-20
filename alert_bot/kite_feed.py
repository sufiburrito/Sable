"""
Real-time price cache backed by Zerodha KiteTicker WebSocket.

KitePriceFeed.start() resolves NSE instrument tokens via kite.ltp(), then opens
a KiteTicker WebSocket in a daemon thread. Incoming ticks update _price_cache.

get_price(yf_symbol) returns the cached price if fresh (< 60 s), falls back to a
REST kite.ltp() call if stale, and returns None if REST also fails — the caller
in main._fetch_one() then tries yfinance.

Thread-safety: all writes to _price_cache and _last_tick_time go through _lock.
"""
import logging
import threading
import time
from typing import Optional

from kiteconnect import KiteConnect, KiteTicker

from alert_bot.kite_auth import get_kite_client

logger = logging.getLogger(__name__)

_STALE_SECONDS = 60   # treat cache entry as stale after this many seconds


def _to_kite_symbol(yf_symbol: str) -> str:
    """'STLTECH.NS' → 'NSE:STLTECH'  (strips .NS suffix, prepends NSE:)"""
    return "NSE:" + yf_symbol.removesuffix(".NS")


class KitePriceFeed:

    def __init__(self, kite: KiteConnect, yf_symbols: list[str]) -> None:
        self._kite = kite
        self._yf_symbols = yf_symbols
        self._price_cache: dict[str, float] = {}
        self._last_tick_time: dict[str, float] = {}
        self._token_to_symbol: dict[int, str] = {}
        self._ticker: Optional[KiteTicker] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Resolve instrument tokens then open the WebSocket. Call once at startup."""
        self._resolve_tokens()
        self._start_websocket()

    def stop(self) -> None:
        """Close the WebSocket cleanly (call on bot shutdown)."""
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None  # Fix 1: clear stale reference so callers can't use a dead object

    def refresh_subscriptions(self, yf_symbols: list[str]) -> None:
        """
        Re-subscribe after stock list changes or daily token renewal.
        Re-auth is handled internally; if it fails, the existing connection is kept.
        """
        # Fix 3: callers no longer pass kite= — re-auth is handled here
        try:
            new_kite = get_kite_client()
            if new_kite is not None:
                with self._lock:
                    self._kite = new_kite
        except Exception as exc:
            logger.warning(
                "Kite re-auth failed during refresh; keeping existing connection: %s", exc
            )

        self._yf_symbols = yf_symbols
        self._resolve_tokens()
        self._start_websocket()

    def get_price(self, yf_symbol: str) -> Optional[float]:
        """
        Return the latest price for a ticker.
        Returns None if unavailable — caller falls back to yfinance.
        """
        kite_symbol = _to_kite_symbol(yf_symbol)

        with self._lock:
            price = self._price_cache.get(kite_symbol)
            last_tick = self._last_tick_time.get(kite_symbol, 0.0)
            kite = self._kite  # Fix 2: capture kite ref under lock to avoid stale reads on refresh

        if price is not None and (time.time() - last_tick) < _STALE_SECONDS:
            return price

        try:
            resp: dict = kite.ltp([kite_symbol])  # type: ignore[assignment]
            return resp[kite_symbol]["last_price"]
        except Exception as exc:
            logger.warning("Kite REST ltp failed for %s: %s", kite_symbol, exc)
            return None

    def _handle_ticks(self, ticks: list) -> None:
        """Process incoming WebSocket ticks. Called by the on_ticks callback."""
        with self._lock:
            for tick in ticks:
                symbol = self._token_to_symbol.get(tick["instrument_token"])
                if symbol:
                    self._price_cache[symbol] = tick["last_price"]
                    self._last_tick_time[symbol] = time.time()

    def _resolve_tokens(self) -> None:
        """Fetch instrument tokens via kite.ltp() and seed the initial price cache."""
        kite_symbols = [_to_kite_symbol(s) for s in self._yf_symbols]
        if not kite_symbols:
            return
        try:
            quotes: dict = self._kite.ltp(kite_symbols)  # type: ignore[assignment]
        except Exception as exc:
            logger.error("Failed to resolve Kite instrument tokens: %s", exc)
            return

        with self._lock:
            self._token_to_symbol = {
                v["instrument_token"]: sym for sym, v in quotes.items()
            }
            for sym, v in quotes.items():
                self._price_cache[sym] = v["last_price"]
                self._last_tick_time[sym] = time.time()

        # Fix 5: warn on any symbols Kite didn't recognise so failures are visible in logs
        resolved_kite = set(quotes.keys())
        requested_kite = set(kite_symbols)
        missing = requested_kite - resolved_kite
        if missing:
            logger.warning(
                "Kite token resolution: %d symbol(s) not found: %s",
                len(missing), sorted(missing),
            )

    def _start_websocket(self) -> None:
        """Open KiteTicker and subscribe all resolved tokens in MODE_LTP."""
        if self._ticker is not None:
            self._ticker.stop()
            time.sleep(0.3)  # Fix 4: brief pause so old WebSocket thread can exit cleanly
            self._ticker = None

        tokens = list(self._token_to_symbol.keys())
        if not tokens:
            logger.warning("No Kite instrument tokens resolved; WebSocket not started")
            return

        self._ticker = KiteTicker(
            api_key=self._kite.api_key,
            access_token=self._kite.access_token,
            reconnect=True,
        )

        def on_connect(ws, _response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_LTP, tokens)
            logger.info("Kite WebSocket connected; subscribed %d symbols", len(tokens))

        self._ticker.on_ticks   = lambda _ws, ticks: self._handle_ticks(ticks)  # type: ignore[assignment]
        self._ticker.on_connect = on_connect  # type: ignore[assignment]
        self._ticker.on_error   = lambda _ws, code, reason: logger.error(  # type: ignore[assignment]
            "Kite WebSocket error %s: %s", code, reason
        )
        self._ticker.connect(threaded=True)
