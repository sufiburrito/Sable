#!/bin/bash
# Cron wrapper for the forward-test rig. Mirrors run_forever.sh: cd into the repo,
# load .env, guard against overlap with flock, log to data/, and notify Discord on
# failure. Pure-Python work — no LLM. Installed by install_forward_cron.sh.
cd "$(dirname "$0")" || exit 1
[ -f .env ] && source .env

LOG="data/forward_test.log"
LOCK="data/forward_test.lock"
mkdir -p data

# Prevent overlapping runs — a slow night shouldn't collide with the next trigger.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date)] forward_test already running — skipping." >> "$LOG"
    exit 0
fi

echo "[$(date)] nightly start" >> "$LOG"
fail=0
# Research-only (experiment mode): snapshot today's live-only contextual factors
# (MMI/VIX/flow/breadth/FII-DII) into datasets.db so they're not lost. Read-only on
# production; `|| true` — a research capture must NEVER affect the production run.
python3 datasets/snapshot_factors.py >> "$LOG" 2>&1 || true
# Forward-test rig first; the journal reads its fresh ledger.
python3 forward_test.py >> "$LOG" 2>&1 || fail=1
# Then rebuild the trade journal (P&L table + missed ledger + Obsidian vault).
python3 -m journal.build >> "$LOG" 2>&1 || fail=1
if [ "$fail" -eq 0 ]; then
    echo "[$(date)] nightly ok" >> "$LOG"
else
    echo "[$(date)] nightly FAILED — see $LOG" >> "$LOG"
    if [ -n "$DISCORD_BROADCAST_WEBHOOK" ]; then
        curl -s -X POST "$DISCORD_BROADCAST_WEBHOOK" \
            -H "Content-Type: application/json" \
            -d '{"content":"⚠️ Nightly forward_test/journal failed — check data/forward_test.log"}' \
            > /dev/null 2>&1
    fi
fi
