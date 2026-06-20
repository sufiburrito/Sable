#!/usr/bin/env python3
"""
forward_test.py — run the COMPLETE forward-test rig in one shot (for cron).

Sequence: backfill (catch newly-fired alerts from sent_alerts.json) → resolve (score
open calls against printed OHLC) → edge (recompute the Bayesian posteriors + δ).

Pure Python — no LLM, no tokens. Each stage is guarded independently so a later
failure can't undo an earlier success (e.g. a resolve still persists even if the
edge report later errors). Exits non-zero if ANY stage failed, so the cron wrapper
can fire a failure notice.

Usage:  python3 forward_test.py
"""
import sys
import time
import traceback
from datetime import datetime

import backfill_ledger
import forward_resolve
import forward_edge


def _stage(name: str, fn) -> bool:
    t0 = time.time()
    print(f"[forward_test] {name} …", flush=True)
    try:
        fn()
        print(f"[forward_test] {name} ok ({time.time() - t0:.1f}s)", flush=True)
        return True
    except Exception:
        print(f"[forward_test] {name} FAILED ({time.time() - t0:.1f}s):\n"
              f"{traceback.format_exc()}", flush=True)
        return False


def main() -> int:
    print(f"=== forward_test {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)
    ok = True
    ok &= _stage("backfill", backfill_ledger.main)
    ok &= _stage("resolve", forward_resolve.main)
    ok &= _stage("edge", forward_edge.main)
    print(f"[forward_test] done — {'all ok' if ok else 'ONE OR MORE STAGES FAILED'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
