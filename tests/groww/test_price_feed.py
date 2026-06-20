from unittest.mock import MagicMock, patch

from alert_bot.price_feed import create_price_feed


def test_create_price_feed_returns_none_when_no_credentials():
    with patch("alert_bot.price_feed.ZERODHA_API_KEY", ""):
        with patch("alert_bot.price_feed.GROWW_API_KEY", ""):
            with patch("alert_bot.price_feed.GROWW_TOTP_SECRET", ""):
                result = create_price_feed(["STLTECH.NS"])

    assert result is None


def test_create_price_feed_returns_kite_feed_when_kite_creds_present():
    mock_kite_client = MagicMock()
    mock_kite_feed = MagicMock()

    with patch("alert_bot.price_feed.ZERODHA_API_KEY", "somekey"):
        with patch("alert_bot.price_feed.GROWW_API_KEY", ""):
            with patch("alert_bot.price_feed.GROWW_TOTP_SECRET", ""):
                with patch("alert_bot.kite_auth.get_kite_client", return_value=mock_kite_client):
                    with patch("alert_bot.kite_feed.KitePriceFeed", return_value=mock_kite_feed):
                        result = create_price_feed(["STLTECH.NS"])

    assert result is mock_kite_feed


def test_create_price_feed_returns_groww_feed_when_only_groww_creds():
    import sys
    mock_groww_client = MagicMock()
    mock_groww_feed = MagicMock()

    # groww_auth and groww_feed don't exist yet (future tasks).
    # Inject fake modules into sys.modules so that the lazy imports inside
    # create_price_feed() succeed and can be patched.
    fake_groww_auth = MagicMock()
    fake_groww_auth.get_groww_client = MagicMock(return_value=mock_groww_client)
    fake_groww_feed_mod = MagicMock()
    fake_groww_feed_mod.GrowwPriceFeed = MagicMock(return_value=mock_groww_feed)

    with patch.dict(sys.modules, {
        "alert_bot.groww_auth": fake_groww_auth,
        "alert_bot.groww_feed": fake_groww_feed_mod,
    }):
        with patch("alert_bot.price_feed.ZERODHA_API_KEY", ""):
            with patch("alert_bot.price_feed.GROWW_API_KEY", "somekey"):
                with patch("alert_bot.price_feed.GROWW_TOTP_SECRET", "somesecret"):
                    result = create_price_feed(["STLTECH.NS"])

    assert result is mock_groww_feed


def test_create_price_feed_kite_wins_when_both_set(caplog):
    import logging
    mock_kite_client = MagicMock()
    mock_kite_feed = MagicMock()

    with patch("alert_bot.price_feed.ZERODHA_API_KEY", "kitekey"):
        with patch("alert_bot.price_feed.GROWW_API_KEY", "growwkey"):
            with patch("alert_bot.price_feed.GROWW_TOTP_SECRET", "secret"):
                with patch("alert_bot.kite_auth.get_kite_client", return_value=mock_kite_client):
                    with patch("alert_bot.kite_feed.KitePriceFeed", return_value=mock_kite_feed):
                        with caplog.at_level(logging.WARNING, logger="alert_bot.price_feed"):
                            result = create_price_feed(["STLTECH.NS"])

    assert result is mock_kite_feed
    assert any("Both Kite and Groww" in r.message for r in caplog.records)


def test_create_price_feed_returns_none_when_kite_client_returns_none():
    with patch("alert_bot.price_feed.ZERODHA_API_KEY", "somekey"):
        with patch("alert_bot.price_feed.GROWW_API_KEY", ""):
            with patch("alert_bot.price_feed.GROWW_TOTP_SECRET", ""):
                with patch("alert_bot.kite_auth.get_kite_client", return_value=None):
                    result = create_price_feed(["STLTECH.NS"])

    assert result is None


def test_create_price_feed_falls_through_to_groww_when_kite_client_fails():
    """If Kite creds are set but get_kite_client() returns None, fall through to Groww."""
    import sys
    from unittest.mock import MagicMock

    mock_groww_client = MagicMock()
    mock_groww_feed = MagicMock()

    fake_groww_auth = MagicMock()
    fake_groww_auth.get_groww_client.return_value = mock_groww_client
    fake_groww_feed_mod = MagicMock()
    fake_groww_feed_mod.GrowwPriceFeed.return_value = mock_groww_feed

    with patch("alert_bot.price_feed.ZERODHA_API_KEY", "kitekey"):
        with patch("alert_bot.price_feed.GROWW_API_KEY", "growwkey"):
            with patch("alert_bot.price_feed.GROWW_TOTP_SECRET", "secret"):
                with patch("alert_bot.kite_auth.get_kite_client", return_value=None):
                    with patch.dict(sys.modules, {
                        "alert_bot.groww_auth": fake_groww_auth,
                        "alert_bot.groww_feed": fake_groww_feed_mod,
                    }):
                        result = create_price_feed(["STLTECH.NS"])

    assert result is mock_groww_feed
