"""
Entry point. Run with:   python run.py
"""
import subprocess, sys, pathlib


def _start_watcher() -> None:
    """Launch watch_flags.py as a background subprocess alongside the bot.

    Silently skipped if the script is missing (e.g. during tests). If the
    watcher fails to start, the loop falls back to inline mtime checks — see
    docs/watch_flags.md § Fallback behaviour.
    """
    script = pathlib.Path(__file__).parent / "watch_flags.py"
    if not script.exists():
        print("[run.py] watch_flags.py not found — file watcher skipped", flush=True)
        return
    subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("[run.py] watch_flags.py started", flush=True)


from alert_bot.main import run

if __name__ == "__main__":
    _start_watcher()
    run()
