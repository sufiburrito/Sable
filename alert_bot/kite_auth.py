"""
Zerodha Kite Connect authentication.

Daily flow: run get_kite_client() at startup and at the market-open daily reload.
If the cached token (data/kite_token.json) is still valid, no network call is made.
If expired or absent, _login_with_totp() automates login via requests + pyotp,
then generate_session() exchanges the request_token for an access_token.

Token expires at 6 AM IST (00:30 UTC) daily — Zerodha's fixed expiry.
All Zerodha credentials are optional; get_kite_client() returns None if any are absent.
"""
import logging
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from kiteconnect import KiteConnect

from alert_bot.config import (
    KITE_TOKEN_FILE,
    ZERODHA_API_KEY,    # used by _login_with_totp (added in Task 3)
    ZERODHA_API_SECRET,
    ZERODHA_PASSWORD,
    ZERODHA_TOTP_SECRET,
    ZERODHA_USER_ID,
)
from alert_bot.token_utils import (
    load_token as _load_token_impl,
    save_token as _save_token_impl,
)

logger = logging.getLogger(__name__)


def load_token() -> Optional[dict]:
    """Return the stored token dict if present and unexpired, else None."""
    return _load_token_impl(KITE_TOKEN_FILE)


def save_token(access_token: str) -> None:
    """Persist access_token with its expiry timestamp to KITE_TOKEN_FILE."""
    _save_token_impl(access_token, KITE_TOKEN_FILE)


def _login_with_totp() -> str:
    """
    Automate Zerodha login + TOTP 2FA via requests (no browser required).
    Returns the request_token from the OAuth redirect Location header.

    Your Kite Connect app's redirect_url (set in https://kite.trade/) can be
    any URL — http://127.0.0.1 works fine; the server doesn't need to be live.
    """
    session = requests.Session()

    resp = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": ZERODHA_USER_ID, "password": ZERODHA_PASSWORD},
    )
    resp.raise_for_status()
    request_id = resp.json()["data"]["request_id"]

    resp = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": ZERODHA_USER_ID,
            "request_id": request_id,
            "twofa_value": pyotp.TOTP(ZERODHA_TOTP_SECRET).now(),
            "twofa_type": "totp",
        },
    )
    resp.raise_for_status()

    login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={ZERODHA_API_KEY}"
    resp = session.get(login_url, allow_redirects=False)
    location = resp.headers.get("Location", "")
    params = parse_qs(urlparse(location).query)
    tokens = params.get("request_token", [])
    if not tokens:
        raise RuntimeError(f"request_token not found in Zerodha redirect: {location!r}")
    return tokens[0]


def get_kite_client() -> Optional[KiteConnect]:
    """
    Return an authenticated KiteConnect instance, or None if credentials absent.

    Uses cached token from KITE_TOKEN_FILE when valid. Runs full TOTP login when
    the token is expired or missing. Safe to call on every daily market-open reload.
    Raises on login failure (network error, wrong credentials, bad TOTP secret).
    """
    if not all([ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID,
                ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET]):
        return None

    token_data = load_token()
    if token_data is None:
        logger.info("Kite access token absent or expired — running TOTP login")
        request_token = _login_with_totp()
        kite = KiteConnect(api_key=ZERODHA_API_KEY)
        session_data: dict = kite.generate_session(request_token, api_secret=ZERODHA_API_SECRET)  # type: ignore[assignment]
        save_token(session_data["access_token"])
        access_token = session_data["access_token"]
        logger.info("Kite TOTP login successful")
    else:
        access_token = token_data["access_token"]

    kite = KiteConnect(api_key=ZERODHA_API_KEY)
    kite.set_access_token(access_token)
    return kite
