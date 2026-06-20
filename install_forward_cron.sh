#!/bin/bash
# Install (or refresh) the nightly forward-test cron in the host user's crontab.
# Idempotent: manages only its own marked line, preserving every other entry.
# Re-run any time to update the schedule. To uninstall:
#   crontab -l | grep -v '# sable-forward-test' | crontab -
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$REPO/run_forward_test.sh"
MARK="# sable-forward-test"
SCHEDULE="30 23 * * 1-5"          # 11:30 PM, Mon–Fri (host TZ = IST), after the day's alerts
LINE="$SCHEDULE cd $REPO && bash $WRAPPER $MARK"

chmod +x "$WRAPPER" "$REPO/forward_test.py" 2>/dev/null || true

echo "Current crontab:"
crontab -l 2>/dev/null || echo "  (none)"

# Rewrite: drop any prior marked line, append the current one.
( crontab -l 2>/dev/null | grep -vF "$MARK" || true; echo "$LINE" ) | crontab -

echo
echo "Installed:"
crontab -l | grep -F "$MARK"
echo
echo "Schedule : $SCHEDULE  (edit SCHEDULE in this script + re-run to change)"
echo "Log      : $REPO/data/forward_test.log"
echo "Uninstall: crontab -l | grep -v '$MARK' | crontab -"
