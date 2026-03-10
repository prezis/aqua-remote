#!/bin/bash
# aqua-remote watchdog — checks heartbeats for all monitored sessions.
# If a monitor's heartbeat is stale (>5 min), restart it and alert.
#
# Install in cron:
#   */5 * * * * bash ~/.aqua-remote/watchdog.sh
#
# This script is copied to ~/.aqua-remote/watchdog.sh during setup.

HEARTBEAT_DIR="$HOME/.aqua-remote/heartbeats"
STATE_DIR="$HOME/.aqua-remote/state"
LOG_DIR="$HOME/.aqua-remote/logs"
LOCKFILE="/tmp/aqua-remote-watchdog.lock"
MAX_AGE=300  # 5 min
AQUA_REMOTE_SRC=""  # Set during install

# Find aqua-remote source
for p in "$HOME/aqua-remote/src" "$HOME/.aqua-remote/src" "$(dirname "$0")/../src"; do
    if [ -f "$p/monitor.py" ]; then
        AQUA_REMOTE_SRC="$p"
        break
    fi
done

if [ -z "$AQUA_REMOTE_SRC" ]; then
    echo "[$(date)] ERROR: Cannot find aqua-remote source" >> "$LOG_DIR/watchdog.log"
    exit 1
fi

mkdir -p "$HEARTBEAT_DIR" "$STATE_DIR" "$LOG_DIR"

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$LOCKFILE" 2>/dev/null || echo 0) ))
    if [ "$LOCK_AGE" -lt 120 ]; then
        exit 0
    fi
    rm -f "$LOCKFILE"
fi
touch "$LOCKFILE"

NOW=$(date +%s)
RESTARTED=0

for hb_file in "$HEARTBEAT_DIR"/*; do
    [ -f "$hb_file" ] || continue

    SESSION_NAME=$(basename "$hb_file")
    LAST_BEAT=$(cat "$hb_file" 2>/dev/null || echo 0)
    AGE=$((NOW - LAST_BEAT))

    if [ "$AGE" -gt "$MAX_AGE" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] STALE heartbeat for $SESSION_NAME (${AGE}s old)" >> "$LOG_DIR/watchdog.log"

        # Read session target from state
        TARGET=$(python3 -c "
import json, sys
try:
    s = json.load(open('$STATE_DIR/${SESSION_NAME}.json'))
    print(s.get('tmux_target', ''))
except: pass
" 2>/dev/null)

        if [ -z "$TARGET" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] No tmux_target for $SESSION_NAME — skipping" >> "$LOG_DIR/watchdog.log"
            continue
        fi

        # Kill old monitor if any
        pkill -f "monitor.py.*--name.*$SESSION_NAME" 2>/dev/null
        sleep 2

        # Restart
        cd "$AQUA_REMOTE_SRC"
        nohup python3 monitor.py --session "$TARGET" --name "$SESSION_NAME" >> "$LOG_DIR/${SESSION_NAME}.log" 2>&1 &
        NEW_PID=$!

        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Restarted $SESSION_NAME (target=$TARGET, PID=$NEW_PID)" >> "$LOG_DIR/watchdog.log"

        # Alert via Python notify
        python3 -c "
from notify import create_channel
ch = create_channel()
ch.send(
    'aqua-remote: $SESSION_NAME — MONITOR RESTARTED',
    'Monitor for <code>$TARGET</code> was dead (heartbeat ${AGE}s old).\n'
    'Auto-restarted as PID $NEW_PID.\n\n'
    'Check if RC link still works.',
)
" 2>/dev/null

        RESTARTED=$((RESTARTED + 1))
    fi
done

rm -f "$LOCKFILE"

if [ "$RESTARTED" -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Watchdog: restarted $RESTARTED monitors" >> "$LOG_DIR/watchdog.log"
fi
