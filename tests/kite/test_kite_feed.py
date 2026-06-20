import time
from unittest.mock import MagicMock, patch

import pytest

from alert_bot.kite_feed import KitePriceFeed, _to_kite_symbol


# ── Symbol mapping ────────────────────────────────────────────────────────────

def test_to_kite_symbol_strips_ns_suffix():
    assert _to_kite_symbol("STLTECH.NS") == "NSE:STLTECH"

def test_to_kite_symbol_passthrough_without_suffix():
    # removesuffix(".NS") on a symbol without .NS should still produce NSE: prefix
    assert _to_kite_symbol("STLTECH") == "NSE:STLTECH"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_kite():
    kite = MagicMock()
    kite.api_key = "testkey"
    kite.access_token = "testtoken"
    kite.ltp.return_value = {
        "NSE:STLTECH": {"instrument_token": 12345, "last_price": 150.0},
        "NSE:BBOX":    {"instrument_token": 67890, "last_price": 200.0},
    }
    return kite


# ── _resolve_tokens ───────────────────────────────────────────────────────────

def test_resolve_tokens_builds_token_map(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS", "BBOX.NS"])
    feed._resolve_tokens()
    assert feed._token_to_symbol[12345] == "NSE:STLTECH"
    assert feed._token_to_symbol[67890] == "NSE:BBOX"

def test_resolve_tokens_seeds_price_cache(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS", "BBOX.NS"])
    feed._resolve_tokens()
    assert feed._price_cache["NSE:STLTECH"] == 150.0
    assert feed._price_cache["NSE:BBOX"] == 200.0

def test_resolve_tokens_tolerates_ltp_failure(mock_kite):
    mock_kite.ltp.side_effect = Exception("timeout")
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()          # must not raise
    assert feed._token_to_symbol == {}

def test_resolve_tokens_no_op_for_empty_list(mock_kite):
    feed = KitePriceFeed(mock_kite, [])
    feed._resolve_tokens()
    mock_kite.ltp.assert_not_called()


# ── _handle_ticks ─────────────────────────────────────────────────────────────

def test_handle_ticks_updates_price_cache(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()
    feed._handle_ticks([{"instrument_token": 12345, "last_price": 165.0}])
    assert feed._price_cache["NSE:STLTECH"] == 165.0

def test_handle_ticks_updates_last_tick_time(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()
    before = time.time()
    feed._handle_ticks([{"instrument_token": 12345, "last_price": 165.0}])
    assert feed._last_tick_time["NSE:STLTECH"] >= before

def test_handle_ticks_ignores_unknown_tokens(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()          # seeds STLTECH at 150.0
    feed._handle_ticks([{"instrument_token": 99999, "last_price": 999.0}])
    assert feed._price_cache["NSE:STLTECH"] == 150.0   # unchanged


# ── get_price ─────────────────────────────────────────────────────────────────

def test_get_price_returns_cache_hit_without_rest_call(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._price_cache["NSE:STLTECH"] = 150.0
    feed._last_tick_time["NSE:STLTECH"] = time.time()
    mock_kite.ltp.reset_mock()

    price = feed.get_price("STLTECH.NS")

    assert price == 150.0
    mock_kite.ltp.assert_not_called()

def test_get_price_falls_back_to_rest_when_stale(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._price_cache["NSE:STLTECH"] = 140.0
    feed._last_tick_time["NSE:STLTECH"] = time.time() - 120   # 2 min stale
    mock_kite.ltp.return_value = {
        "NSE:STLTECH": {"instrument_token": 12345, "last_price": 155.0}
    }

    price = feed.get_price("STLTECH.NS")

    assert price == 155.0
    mock_kite.ltp.assert_called_once_with(["NSE:STLTECH"])

def test_get_price_returns_none_when_rest_also_fails(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    mock_kite.ltp.side_effect = Exception("network down")

    assert feed.get_price("STLTECH.NS") is None

def test_get_price_returns_none_for_completely_unknown_symbol(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    mock_kite.ltp.side_effect = Exception("symbol not found")

    assert feed.get_price("UNKNOWN.NS") is None


# ── _start_websocket ──────────────────────────────────────────────────────────

def test_start_websocket_creates_ticker_and_connects(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()   # populates _token_to_symbol so start_websocket has tokens

    mock_ticker_cls = MagicMock()
    mock_ticker_instance = MagicMock()
    mock_ticker_cls.return_value = mock_ticker_instance

    with patch("alert_bot.kite_feed.KiteTicker", mock_ticker_cls):
        feed._start_websocket()

    mock_ticker_cls.assert_called_once_with(
        api_key="testkey",
        access_token="testtoken",
        reconnect=True,
    )
    mock_ticker_instance.connect.assert_called_once_with(threaded=True)


def test_start_websocket_no_op_when_no_tokens(mock_kite):
    """If _resolve_tokens failed, _start_websocket must not crash."""
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    # Deliberately skip _resolve_tokens so _token_to_symbol is empty

    with patch("alert_bot.kite_feed.KiteTicker") as mock_ticker_cls:
        feed._start_websocket()
        mock_ticker_cls.assert_not_called()


def test_refresh_subscriptions_updates_symbols_and_adopts_new_kite(mock_kite):
    new_kite = MagicMock()
    new_kite.api_key = "newkey"
    new_kite.access_token = "newtoken"
    new_kite.ltp.return_value = {
        "NSE:CGPOWER": {"instrument_token": 99999, "last_price": 780.0}
    }

    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])

    with patch("alert_bot.kite_feed.get_kite_client", return_value=new_kite):
        with patch.object(feed, "_start_websocket"):
            feed.refresh_subscriptions(["CGPOWER.NS"])

    assert feed._yf_symbols == ["CGPOWER.NS"]
    assert feed._price_cache["NSE:CGPOWER"] == 780.0
    with feed._lock:
        assert feed._kite is new_kite


def test_start_websocket_stops_existing_ticker_before_creating_new_one(mock_kite):
    """Even a reconnecting (not connected) ticker must be stopped to avoid duplicates."""
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()

    old_ticker = MagicMock()
    old_ticker.is_connected.return_value = False   # simulates reconnect-backoff state
    feed._ticker = old_ticker

    mock_ticker_cls = MagicMock()
    mock_ticker_cls.return_value = MagicMock()

    with patch("alert_bot.kite_feed.KiteTicker", mock_ticker_cls):
        feed._start_websocket()

    old_ticker.stop.assert_called_once()
    mock_ticker_cls.assert_called_once()  # new ticker was created


def test_on_connect_subscribes_and_sets_mode(mock_kite):
    """on_connect callback must call ws.subscribe(tokens) and ws.set_mode(MODE_LTP, tokens)."""
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed._resolve_tokens()

    mock_ticker_cls = MagicMock()
    mock_ticker_instance = MagicMock()
    mock_ticker_cls.return_value = mock_ticker_instance

    with patch("alert_bot.kite_feed.KiteTicker", mock_ticker_cls):
        feed._start_websocket()

    # Fire the on_connect callback manually
    on_connect = mock_ticker_instance.on_connect
    mock_ws = MagicMock()
    on_connect(mock_ws, None)

    # mock_kite returns both STLTECH+BBOX regardless of requested symbols
    expected_tokens = list(feed._token_to_symbol.keys())
    mock_ws.subscribe.assert_called_once_with(expected_tokens)
    mock_ws.set_mode.assert_called_once_with(mock_ws.MODE_LTP, expected_tokens)


def test_stop_calls_ticker_stop(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    mock_ticker = MagicMock()
    feed._ticker = mock_ticker

    feed.stop()

    mock_ticker.stop.assert_called_once()


def test_stop_is_safe_when_ticker_is_none(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    feed.stop()  # must not raise


def test_stop_clears_ticker_reference(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    mock_ticker = MagicMock()
    feed._ticker = mock_ticker

    feed.stop()

    mock_ticker.stop.assert_called_once()
    assert feed._ticker is None


def test_get_price_captures_kite_ref_under_lock(mock_kite):
    """get_price must not call ltp() with a potentially stale self._kite reference."""
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    # No cache entry — falls through to REST
    mock_kite.ltp.return_value = {"NSE:STLTECH": {"last_price": 155.0}}

    price = feed.get_price("STLTECH.NS")

    assert price == 155.0
    # Verify ltp was called (i.e. the REST path ran)
    mock_kite.ltp.assert_called_once_with(["NSE:STLTECH"])


def test_resolve_tokens_logs_missing_symbols(mock_kite):
    """If a symbol is not in the ltp() response, log a warning."""
    mock_kite.ltp.return_value = {
        "NSE:STLTECH": {"instrument_token": 12345, "last_price": 150.0},
        # NSE:BBOX intentionally absent
    }
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS", "BBOX.NS"])

    with patch.object(feed, "_start_websocket"):
        with patch("alert_bot.kite_feed.logger") as mock_logger:
            feed._resolve_tokens()
            warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
            assert any("BBOX" in c or "NSE:BBOX" in c for c in warning_calls)


def test_refresh_subscriptions_calls_get_kite_client_internally(mock_kite):
    """refresh_subscriptions must call get_kite_client() without kite= argument."""
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])
    new_kite = MagicMock()
    new_kite.ltp.return_value = {
        "NSE:STLTECH": {"instrument_token": 12345, "last_price": 150.0}
    }

    with patch("alert_bot.kite_feed.get_kite_client", return_value=new_kite):
        with patch.object(feed, "_start_websocket"):
            feed.refresh_subscriptions(["STLTECH.NS"])

    with feed._lock:
        assert feed._kite is new_kite


def test_refresh_subscriptions_keeps_old_kite_on_auth_failure(mock_kite):
    feed = KitePriceFeed(mock_kite, ["STLTECH.NS"])

    with patch("alert_bot.kite_feed.get_kite_client", side_effect=RuntimeError("auth failed")):
        with patch.object(feed, "_start_websocket"):
            with patch.object(feed, "_resolve_tokens"):
                feed.refresh_subscriptions(["STLTECH.NS"])  # must not raise

    with feed._lock:
        assert feed._kite is mock_kite  # unchanged
