#!/usr/bin/env python3
"""
journal/build.py — rebuild the whole trade journal in one shot (for the nightly cron).

Sequence: realized_pnl (FIFO → portfolio.db.closed_lots) → missed_trades (→ the missed
ledger artifact + summary) → obsidian (→ the journal/vault/ notes + dashboards, incl. the
Tax Planning view) → tax_reminders (→ idempotent Discord nudges + CalDAV LTCG-crossing events).
Pure Python, no LLM. Runs AFTER forward_test.py (the journal reads a fresh forward
ledger). Each stage is guarded so a later failure can't undo an earlier success;
exits non-zero if any stage failed.

Usage:  python3 -m journal.build
"""
import sys
import time
import traceback
from datetime import datetime

from journal import pnl_statement, realized_pnl, missed_trades, execution_review, obsidian, tax_reminders


def _stage(name: str, fn) -> bool:
    t0 = time.time()
    print(f"[journal] {name} …", flush=True)
    try:
        fn()
        print(f"[journal] {name} ok ({time.time() - t0:.1f}s)", flush=True)
        return True
    except Exception:
        print(f"[journal] {name} FAILED ({time.time() - t0:.1f}s):\n"
              f"{traceback.format_exc()}", flush=True)
        return False


def main() -> int:
    print(f"=== journal build {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)
    ok = True
    ok &= _stage("charge_model", pnl_statement.main)
    ok &= _stage("realized_pnl", realized_pnl.main)
    ok &= _stage("missed_trades", missed_trades.main)
    ok &= _stage("execution_review", execution_review.main)
    ok &= _stage("obsidian_vault", obsidian.main)
    ok &= _stage("tax_reminders", tax_reminders.main)
    print(f"[journal] done — {'all ok' if ok else 'ONE OR MORE STAGES FAILED'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
