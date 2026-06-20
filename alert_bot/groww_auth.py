"""
Groww Trade API authentication.

Daily flow identical to kite_auth: token expires at 6 AM IST, cached in
data/groww_token.json, only renewed when absent or expired.
get_groww_client() returns None if credentials are absent from .env.
Raises on auth failure (network error, bad credentials, wrong TOTP secret).
"""
import logging
from typing import Optional

import pyotp
from growwapi import GrowwAPI

from alert_bot.config import (
    GROWW_API_KEY,
    GROWW_TOTP_SECRET,
    GROWW_TOKEN_FILE,
)
from alert_bot.token_utils import load_token, save_token

logger = logging.getLogger(__name__)


def _do_login() -> str:
    """Generate TOTP and call GrowwAPI.get_access_token. Returns the access token string."""
    totp_code = pyotp.TOTP(GROWW_TOTP_SECRET).now()
    result = GrowwAPI.get_access_token(api_key=GROWW_API_KEY, totp=totp_code)
    return result["token"]


def get_groww_client() -> Optional[GrowwAPI]:
    """
    Return an authenticated GrowwAPI instance, or None if credentials absent.
    Uses cached token when valid; runs TOTP login when expired or missing.
    Safe to call at startup and on every daily market-open reload.
    """
    if not all([GROWW_API_KEY, GROWW_TOTP_SECRET]):
        return None

    token_data = load_token(GROWW_TOKEN_FILE)
    if token_data is None:
        logger.info("Groww access token absent or expired — running TOTP login")
        access_token = _do_login()
        save_token(access_token, GROWW_TOKEN_FILE)
        logger.info("Groww TOTP login successful")
    else:
        access_token = token_data["access_token"]

    return GrowwAPI(access_token)
