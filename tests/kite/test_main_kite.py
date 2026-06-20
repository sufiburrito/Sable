from unittest.mock import MagicMock, patch

import alert_bot.main as main_module


def test_fetch_one_returns_feed_price_when_available(monkeypatch):
    mock_feed = MagicMock()
    mock_feed.get_price.return_value = 155.0
    monkeypatch.setattr(main_module, "_price_feed", mock_feed)

    price = main_module._fetch_one("STLTECH.NS")

    assert price == 155.0
    mock_feed.get_price.assert_called_once_with("STLTECH.NS")


def test_fetch_one_does_not_call_yfinance_when_feed_succeeds(monkeypatch):
    mock_feed = MagicMock()
    mock_feed.get_price.return_value = 155.0
    monkeypatch.setattr(main_module, "_price_feed", mock_feed)

    with patch("alert_bot.main.yf") as mock_yf:
        main_module._fetch_one("STLTECH.NS")
        mock_yf.Ticker.assert_not_called()


def test_fetch_one_falls_back_to_yfinance_when_feed_returns_none(monkeypatch):
    mock_feed = MagicMock()
    mock_feed.get_price.return_value = None
    monkeypatch.setattr(main_module, "_price_feed", mock_feed)

    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 160.0
    with patch("alert_bot.main.yf.Ticker", return_value=mock_ticker):
        price = main_module._fetch_one("STLTECH.NS")

    assert price == 160.0


def test_fetch_one_falls_back_to_yfinance_when_feed_not_configured(monkeypatch):
    monkeypatch.setattr(main_module, "_price_feed", None)

    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 160.0
    with patch("alert_bot.main.yf.Ticker", return_value=mock_ticker):
        price = main_module._fetch_one("STLTECH.NS")

    assert price == 160.0
