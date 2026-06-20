#!/bin/bash
cd "$(dirname "$0")"
source .env
LOG="data/crashes.log"

echo "run_forever.sh running (PID $$)"

trap 'echo "[$(date)] Interrupted. Shutting down." | tee -a "$LOG"; kill $BOT_PID 2>/dev/null; exit 0' INT TERM

# Crash notification → #sable-broadcast via webhook. Out-of-process and runs even
# when the bot is down, so it can't use the bot gateway.
send_discord() {
    [ -z "$DISCORD_BROADCAST_WEBHOOK" ] && return 0
    curl -s -X POST \
        "$DISCORD_BROADCAST_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$(printf '{"content": %s}' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
        > /dev/null 2>&1
}

while true; do
    echo "[$(date)] Starting bot..." | tee -a "$LOG"
    python3 run.py &
    BOT_PID=$!
    wait $BOT_PID
    code=$?

    if [ $code -eq 0 ]; then
        echo "[$(date)] Bot exited cleanly (code 0). Stopping." | tee -a "$LOG"
        break
    fi

    msg="[$(date)] Bot crashed with exit code $code. Restarting in 5s..."
    echo "$msg" | tee -a "$LOG"
    send_discord "⚠️ Bot crashed (exit code $code). Restarting in 5 seconds..."
    sleep 5
done
