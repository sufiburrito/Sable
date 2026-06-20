import json
import time
from unittest.mock import MagicMock, patch

import pytest

import alert_bot.kite_auth as kite_auth


@pytest.fixture
def tmp_token_file(tmp_path, monkeypatch):
    path = tmp_path / "kite_token.json"
    monkeypatch.setattr(kite_auth, "KITE_TOKEN_FILE", path)
    return path


def test_load_token_returns_none_when_file_missing(tmp_token_file):
    assert kite_auth.load_token() is None


def test_load_token_returns_none_when_expired(tmp_token_file):
    tmp_token_file.write_text(json.dumps({
        "access_token": "abc",
        "expires_at": time.time() - 1,
    }))
    assert kite_auth.load_token() is None


def test_load_token_returns_data_when_valid(tmp_token_file):
    tmp_token_file.write_text(json.dumps({
        "access_token": "abc",
        "expires_at": time.time() + 3600,
    }))
    data = kite_auth.load_token()
    assert data is not None
    assert data["access_token"] == "abc"


def test_load_token_returns_none_when_file_corrupt(tmp_token_file):
    tmp_token_file.write_text("not valid json{{{")
    assert kite_auth.load_token() is None


def test_save_and_reload_roundtrip(tmp_token_file):
    kite_auth.save_token("roundtrip_token")
    data = kite_auth.load_token()
    assert data is not None
    assert data["access_token"] == "roundtrip_token"


def test_save_token_sets_future_expiry(tmp_token_file):
    kite_auth.save_token("mytoken")
    raw = json.loads(tmp_token_file.read_text())
    assert raw["expires_at"] > time.time()


def test_get_kite_client_returns_none_when_no_credentials(monkeypatch):
    for attr in ["ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_USER_ID",
                 "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET"]:
        monkeypatch.setattr(kite_auth, attr, "")
    assert kite_auth.get_kite_client() is None


def test_get_kite_client_uses_cached_token_without_calling_login(
        tmp_token_file, monkeypatch):
    tmp_token_file.write_text(json.dumps({
        "access_token": "cached_token",
        "expires_at": time.time() + 3600,
    }))
    for attr, val in [("ZERODHA_API_KEY", "k"), ("ZERODHA_API_SECRET", "s"),
                      ("ZERODHA_USER_ID", "U"), ("ZERODHA_PASSWORD", "p"),
                      ("ZERODHA_TOTP_SECRET", "T")]:
        monkeypatch.setattr(kite_auth, attr, val)

    mock_kite_cls = MagicMock()
    mock_instance = MagicMock()
    mock_kite_cls.return_value = mock_instance

    with patch("alert_bot.kite_auth.KiteConnect", mock_kite_cls):
        with patch.object(kite_auth, "_login_with_totp") as mock_login:
            client = kite_auth.get_kite_client()
            mock_login.assert_not_called()

    mock_instance.set_access_token.assert_called_once_with("cached_token")
    assert client is mock_instance


def test_get_kite_client_calls_login_when_token_absent(tmp_token_file, monkeypatch):
    for attr, val in [("ZERODHA_API_KEY", "k"), ("ZERODHA_API_SECRET", "s"),
                      ("ZERODHA_USER_ID", "U"), ("ZERODHA_PASSWORD", "p"),
                      ("ZERODHA_TOTP_SECRET", "T")]:
        monkeypatch.setattr(kite_auth, attr, val)

    mock_kite_cls = MagicMock()
    mock_instance = MagicMock()
    mock_instance.generate_session.return_value = {"access_token": "new_token"}
    mock_kite_cls.return_value = mock_instance

    with patch("alert_bot.kite_auth.KiteConnect", mock_kite_cls):
        with patch.object(kite_auth, "_login_with_totp", return_value="req_tok") as mock_login:
            client = kite_auth.get_kite_client()
            mock_login.assert_called_once()

    mock_instance.generate_session.assert_called_once_with("req_tok", api_secret="s")
    mock_instance.set_access_token.assert_called_once_with("new_token")
    assert tmp_token_file.exists()
    assert json.loads(tmp_token_file.read_text())["access_token"] == "new_token"


def test_login_with_totp_extracts_request_token(monkeypatch):
    monkeypatch.setattr(kite_auth, "ZERODHA_API_KEY", "myapikey")
    monkeypatch.setattr(kite_auth, "ZERODHA_USER_ID", "AB1234")
    monkeypatch.setattr(kite_auth, "ZERODHA_PASSWORD", "pass")
    monkeypatch.setattr(kite_auth, "ZERODHA_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

    mock_session = MagicMock()

    login_resp = MagicMock()
    login_resp.json.return_value = {"data": {"request_id": "rid123"}}
    login_resp.raise_for_status = MagicMock()

    twofa_resp = MagicMock()
    twofa_resp.raise_for_status = MagicMock()

    mock_session.post.side_effect = [login_resp, twofa_resp]

    redirect_resp = MagicMock()
    redirect_resp.headers = {
        "Location": "http://127.0.0.1/?request_token=abc123&action=login&status=success"
    }
    mock_session.get.return_value = redirect_resp

    with patch("alert_bot.kite_auth.requests.Session", return_value=mock_session):
        with patch("alert_bot.kite_auth.pyotp.TOTP") as mock_totp_cls:
            mock_totp_cls.return_value.now.return_value = "123456"
            token = kite_auth._login_with_totp()

    assert token == "abc123"
    mock_totp_cls.assert_called_once_with("JBSWY3DPEHPK3PXP")
