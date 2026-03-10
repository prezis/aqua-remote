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
PID_DIR="$HOME/.aqua-remote/pids"
LOCKFILE="/tmp/aqua-remote-watchdog.lock"
MAX_AGE=300  # 5 min
AQUA_REMOTE_SRC=""  # Set during install

# OS detection for stat command compatibility
get_file_mtime() {
    if [[ "$(uname)" == "Darwin" ]]; then
        stat -f %m "$1" 2>/dev/null || echo 0
    else
        stat -c %Y "$1" 2>/dev/null || echo 0
    fi
}

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

mkdir -p "$HEARTBEAT_DIR" "$STATE_DIR" "$LOG_DIR" "$PID_DIR"

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
    LOCK_AGE=$(( $(date +%s) - $(get_file_mtime "$LOCKFILE") ))
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

        # Read session target from state (pass paths via env to avoid injection)
        TARGET=$(AQUA_STATE_DIR="$STATE_DIR" AQUA_SESSION="$SESSION_NAME" python3 -c "
import json, os
try:
    state_dir = os.environ['AQUA_STATE_DIR']
    session = os.environ['AQUA_SESSION']
    s = json.load(open(os.path.join(state_dir, session + '.json')))
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

        # Alert via Python notify (pass values via env to avoid shell injection)
        AQUA_SESSION="$SESSION_NAME" AQUA_TARGET="$TARGET" AQUA_AGE="$AGE" AQUA_PID="$NEW_PID" \
        python3 -c "
import os
from notify import create_channel
ch = create_channel()
name = os.environ['AQUA_SESSION']
target = os.environ['AQUA_TARGET']
age = os.environ['AQUA_AGE']
pid = os.environ['AQUA_PID']
ch.send(
    f'aqua-remote: {name} — MONITOR RESTARTED',
    f'Monitor for <code>{target}</code> was dead (heartbeat {age}s old).\n'
    f'Auto-restarted as PID {pid}.\n\n'
    f'Check if RC link still works.',
)
" 2>/dev/null

        RESTARTED=$((RESTARTED + 1))
    fi
done

rm -f "$LOCKFILE"

if [ "$RESTARTED" -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Watchdog: restarted $RESTARTED monitors" >> "$LOG_DIR/watchdog.log"
fi
