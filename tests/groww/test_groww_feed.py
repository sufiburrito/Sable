import time
from unittest.mock import MagicMock, patch

import pytest
from alert_bot.groww_feed import GrowwPriceFeed, _to_trading_symbol


# ── Symbol mapping ────────────────────────────────────────────────────────────

def test_to_trading_symbol_strips_ns():
    assert _to_trading_symbol("STLTECH.NS") == "STLTECH"

def test_to_trading_symbol_unchanged_without_ns():
    assert _to_trading_symbol("STLTECH") == "STLTECH"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_groww():
    groww = MagicMock()
    groww.get_instrument_by_exchange_and_trading_symbol.side_effect = lambda exchange, trading_symbol: {  # noqa: ARG005
        "exchange_token": 2885 if trading_symbol == "STLTECH" else 1234,
        "trading_symbol": trading_symbol,
    }
    # REST ltp for fallback
    groww.get_ltp.return_value = {"STLTECH": {"ltp": 152.0}}
    return groww


# ── _resolve_tokens ───────────────────────────────────────────────────────────

def test_resolve_tokens_builds_token_map(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()
    assert feed._token_to_symbol["2885"] == "STLTECH.NS"


def test_resolve_tokens_normalises_token_to_str(mock_groww):
    """exchange_token from SDK may be int — must be stored as str."""
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()
    for key in feed._token_to_symbol:
        assert isinstance(key, str)


def test_resolve_tokens_builds_instrument_list(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()
    assert len(feed._instrument_list) == 1
    item = feed._instrument_list[0]
    assert item["exchange"] == "NSE"
    assert item["segment"] == "CASH"
    assert item["exchange_token"] == "2885"


def test_resolve_tokens_tolerates_lookup_failure(mock_groww):
    mock_groww.get_instrument_by_exchange_and_trading_symbol.side_effect = Exception("not found")
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()  # must not raise
    assert feed._token_to_symbol == {}


def test_resolve_tokens_logs_missing_symbols(mock_groww, caplog):
    import logging
    mock_groww.get_instrument_by_exchange_and_trading_symbol.side_effect = Exception("not found")
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    with caplog.at_level(logging.WARNING):
        feed._resolve_tokens()
    assert any("STLTECH" in r.message for r in caplog.records)


# ── get_price ─────────────────────────────────────────────────────────────────

def test_get_price_returns_cached_price_without_rest(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._price_cache["STLTECH.NS"] = 150.0
    feed._last_tick_time["STLTECH.NS"] = time.time()
    mock_groww.get_ltp.reset_mock()

    price = feed.get_price("STLTECH.NS")

    assert price == 150.0
    mock_groww.get_ltp.assert_not_called()


def test_get_price_falls_back_to_rest_when_stale(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._price_cache["STLTECH.NS"] = 140.0
    feed._last_tick_time["STLTECH.NS"] = time.time() - 120  # stale

    price = feed.get_price("STLTECH.NS")

    assert price == 152.0
    mock_groww.get_ltp.assert_called_once_with(("STLTECH",), segment="CASH")


def test_get_price_returns_none_when_rest_fails(mock_groww):
    mock_groww.get_ltp.side_effect = Exception("network error")
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])

    assert feed.get_price("STLTECH.NS") is None


def test_get_price_returns_none_when_no_cache_and_rest_fails(mock_groww):
    mock_groww.get_ltp.side_effect = Exception("network error")
    feed = GrowwPriceFeed(mock_groww, ["UNKNOWN.NS"])

    assert feed.get_price("UNKNOWN.NS") is None


# ── _start_feed / on_data_received / stop / refresh ──────────────────────────

def test_start_feed_creates_groww_feed_and_starts_consume_thread(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()

    mock_groww_feed_cls = MagicMock()
    mock_groww_feed_instance = MagicMock()
    mock_groww_feed_cls.return_value = mock_groww_feed_instance
    mock_groww_feed_instance.consume.return_value = None

    with patch("alert_bot.groww_feed.GrowwFeed", mock_groww_feed_cls):
        feed._start_feed()

    mock_groww_feed_cls.assert_called_once_with(mock_groww)
    mock_groww_feed_instance.subscribe_ltp.assert_called_once()
    assert feed._consume_thread is not None
    assert feed._consume_thread.daemon is True


def test_start_feed_no_op_when_no_tokens(mock_groww):
    """If _resolve_tokens found nothing, _start_feed must not crash."""
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    # Don't call _resolve_tokens — _token_to_symbol stays empty

    with patch("alert_bot.groww_feed.GrowwFeed") as mock_groww_feed_cls:
        feed._start_feed()
        mock_groww_feed_cls.assert_not_called()


def test_on_data_received_updates_price_cache(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()

    mock_groww_feed_cls = MagicMock()
    mock_groww_feed_instance = MagicMock()
    mock_groww_feed_cls.return_value = mock_groww_feed_instance
    mock_groww_feed_instance.consume.return_value = None

    mock_groww_feed_instance.get_ltp.return_value = {
        "NSE": {"CASH": {"2885": {"ltp": 165.0}}}
    }

    captured_callback = {}

    def fake_subscribe_ltp(_instrument_list, on_data_received=None):
        captured_callback["fn"] = on_data_received

    mock_groww_feed_instance.subscribe_ltp.side_effect = fake_subscribe_ltp

    with patch("alert_bot.groww_feed.GrowwFeed", mock_groww_feed_cls):
        feed._start_feed()

    meta = {"exchange": "NSE", "segment": "CASH", "feed_key": "2885", "feed_type": "ltp"}
    captured_callback["fn"](meta)

    assert feed._price_cache.get("STLTECH.NS") == 165.0
    assert feed._last_tick_time.get("STLTECH.NS", 0) > 0


def test_stop_calls_unsubscribe_and_stops_loop(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed._resolve_tokens()

    mock_groww_feed_cls = MagicMock()
    mock_groww_feed_instance = MagicMock()
    mock_groww_feed_cls.return_value = mock_groww_feed_instance
    mock_groww_feed_instance.consume.return_value = None
    mock_groww_feed_instance._nats_client._loop.is_running.return_value = False

    with patch("alert_bot.groww_feed.GrowwFeed", mock_groww_feed_cls):
        feed._start_feed()
        feed.stop()

    mock_groww_feed_instance.unsubscribe_ltp.assert_called_once()
    assert feed._groww_feed is None


def test_stop_is_safe_when_feed_is_none(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])
    feed.stop()  # must not raise


def test_refresh_subscriptions_keeps_old_connection_on_auth_failure(mock_groww):
    feed = GrowwPriceFeed(mock_groww, ["STLTECH.NS"])

    with patch("alert_bot.groww_feed.GrowwFeed") as mock_feed_cls:
        mock_feed_cls.return_value.consume.return_value = None
        with patch("alert_bot.groww_auth.get_groww_client", side_effect=RuntimeError("auth down")):
            with patch.object(feed, "_start_feed"):
                with patch.object(feed, "_resolve_tokens"):
                    feed.refresh_subscriptions(["STLTECH.NS"])  # must not raise

    with feed._lock:
        assert feed._groww is mock_groww  # unchanged
