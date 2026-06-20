import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alert_bot import token_utils


@pytest.fixture
def token_file(tmp_path) -> Path:
    return tmp_path / "test_token.json"


def test_load_token_returns_none_when_file_missing(token_file):
    assert token_utils.load_token(token_file) is None


def test_load_token_returns_none_when_expired(token_file):
    token_file.write_text(json.dumps({
        "access_token": "abc",
        "expires_at": time.time() - 1,
    }))
    assert token_utils.load_token(token_file) is None


def test_load_token_returns_data_when_valid(token_file):
    token_file.write_text(json.dumps({
        "access_token": "abc",
        "expires_at": time.time() + 3600,
    }))
    data = token_utils.load_token(token_file)
    assert data is not None
    assert data["access_token"] == "abc"


def test_load_token_returns_none_on_corrupt_file(token_file):
    token_file.write_text("not valid json{{")
    assert token_utils.load_token(token_file) is None


def test_save_and_reload_roundtrip(token_file):
    token_utils.save_token("mytoken", token_file)
    data = token_utils.load_token(token_file)
    assert data is not None
    assert data["access_token"] == "mytoken"


def test_save_token_sets_future_expiry(token_file):
    token_utils.save_token("mytoken", token_file)
    raw = json.loads(token_file.read_text())
    assert raw["expires_at"] > time.time()


def test_save_token_is_atomic(token_file):
    """No .tmp file should remain after save_token completes."""
    token_utils.save_token("mytoken", token_file)
    tmp_files = list(token_file.parent.glob("*.tmp"))
    assert tmp_files == []


def test_save_token_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "token.json"
    token_utils.save_token("mytoken", nested)
    assert nested.exists()


def test_token_expiry_is_next_0030_utc():
    before = datetime.now(timezone.utc)
    ts = token_utils._token_expiry_ts()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert dt.hour == 0
    assert dt.minute == 30
    assert dt.second == 0
    assert ts > before.timestamp()
