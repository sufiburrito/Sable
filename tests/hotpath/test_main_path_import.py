"""
Hot-path regression: alert_bot.main must import `Path` so the runtime call
sites (_refresh_nifty_cache at module line ~279, nightly refresh at ~959) do
not raise NameError.

Guard for bean algotrading-f5o8 — main.py used pathlib.Path at two call sites
and one annotation but never imported it. Python 3.14's lazy annotation
evaluation hid the bug at import time; the daily Nifty cache refresh crashed
with NameError every time it ran.
"""
import alert_bot.main as main


def test_path_is_importable_in_main():
    # The symbol must resolve in the module namespace, or every Path() call
    # site raises NameError at runtime.
    assert hasattr(main, "Path")


def test_refresh_nifty_cache_does_not_raise_nameerror(monkeypatch):
    # Force the no-data branch so we exercise the Path() line (line ~279)
    # without a network call. Before the fix this raised NameError *before*
    # ever reaching the try/except.
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: None)

    # Must complete cleanly (logs a "no data" warning), not raise.
    main._refresh_nifty_cache()
