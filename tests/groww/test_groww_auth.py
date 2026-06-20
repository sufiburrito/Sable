import json
import time
from unittest.mock import MagicMock, patch

import pytest

import alert_bot.groww_auth as groww_auth


@pytest.fixture
def tmp_token_file(tmp_path, monkeypatch):
    path = tmp_path / "groww_token.json"
    monkeypatch.setattr(groww_auth, "GROWW_TOKEN_FILE", path)
    return path


def test_get_groww_client_returns_none_when_no_credentials(monkeypatch):
    monkeypatch.setattr(groww_auth, "GROWW_API_KEY", "")
    monkeypatch.setattr(groww_auth, "GROWW_TOTP_SECRET", "")
    assert groww_auth.get_groww_client() is None


def test_get_groww_client_returns_none_when_only_api_key(monkeypatch):
    monkeypatch.setattr(groww_auth, "GROWW_API_KEY", "somekey")
    monkeypatch.setattr(groww_auth, "GROWW_TOTP_SECRET", "")
    assert groww_auth.get_groww_client() is None


def test_get_groww_client_uses_cached_token_without_login(tmp_token_file, monkeypatch):
    tmp_token_file.write_text(json.dumps({
        "access_token": "cached_token",
        "expires_at": time.time() + 3600,
    }))
    monkeypatch.setattr(groww_auth, "GROWW_API_KEY", "key")
    monkeypatch.setattr(groww_auth, "GROWW_TOTP_SECRET", "SECRET")

    mock_groww_cls = MagicMock()
    mock_instance = MagicMock()
    mock_groww_cls.return_value = mock_instance

    with patch("alert_bot.groww_auth.GrowwAPI", mock_groww_cls):
        with patch.object(groww_auth, "_do_login") as mock_login:
            client = groww_auth.get_groww_client()
            mock_login.assert_not_called()

    mock_groww_cls.assert_called_once_with("cached_token")
    assert client is mock_instance


def test_get_groww_client_calls_login_when_token_absent(tmp_token_file, monkeypatch):
    monkeypatch.setattr(groww_auth, "GROWW_API_KEY", "key")
    monkeypatch.setattr(groww_auth, "GROWW_TOTP_SECRET", "SECRET")

    mock_groww_cls = MagicMock()
    mock_instance = MagicMock()
    mock_groww_cls.return_value = mock_instance

    with patch("alert_bot.groww_auth.GrowwAPI", mock_groww_cls):
        with patch.object(groww_auth, "_do_login", return_value="new_token") as mock_login:
            result = groww_auth.get_groww_client()
            mock_login.assert_called_once()

    assert result is mock_instance
    mock_groww_cls.assert_called_once_with("new_token")
    assert tmp_token_file.exists()
    assert json.loads(tmp_token_file.read_text())["access_token"] == "new_token"


def test_do_login_extracts_token_from_get_access_token(monkeypatch):
    monkeypatch.setattr(groww_auth, "GROWW_API_KEY", "myapikey")
    monkeypatch.setattr(groww_auth, "GROWW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

    mock_groww_cls = MagicMock()
    mock_groww_cls.get_access_token.return_value = {"token": "fresh_access_token"}

    with patch("alert_bot.groww_auth.GrowwAPI", mock_groww_cls):
        with patch("alert_bot.groww_auth.pyotp.TOTP") as mock_totp_cls:
            mock_totp_cls.return_value.now.return_value = "123456"
            token = groww_auth._do_login()

    assert token == "fresh_access_token"
    mock_totp_cls.assert_called_once_with("JBSWY3DPEHPK3PXP")
    mock_groww_cls.get_access_token.assert_called_once_with(
        api_key="myapikey", totp="123456"
    )
