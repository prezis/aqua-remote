#!/usr/bin/env python3
"""
aqua-remote monitor — watches a Claude Code tmux session, auto-recovers RC.

Usage:
    python3 monitor.py --session sol:0 --name pilot
    python3 monitor.py --session work:2 --name backend

Features:
- Detects idle sessions (no meaningful output change)
- Detects "RC reconnecting" state
- Auto-recovery via RC menu (Claude Code has NO /disconnect command!)
- Sends new RC link to configured notification channel
- Heartbeat file for external watchdog
- Session name in all alerts

Recovery strategy (learned the hard way):
- Claude Code has NO /disconnect command — sending it causes "Unknown skill" spam.
- To disconnect: send /remote-control → wait for menu → Up Up Enter (selects Disconnect).
- To reconnect: send /remote-control → wait for menu → Enter (Continue is default).
- For "reconnecting" state: Ctrl+C + Escape to clear, then fresh /remote-control.
- NEVER send tmux keys directly to a window with an active Claude session from
  the same window — open a helper window instead.
- Bridge pointer cleanup: rm ~/.claude/projects/*/bridge-pointer.json helps clear
  stale reconnecting state.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure src/ is on path regardless of CWD
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

from notify import create_channel, load_config, NotifyChannel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHECK_INTERVAL = 15  # seconds between checks
DISCONNECT_THRESHOLD = 120  # 2 min idle = trigger recovery
HEARTBEAT_INTERVAL = 1800  # 30 min between logged heartbeats
MAX_RECOVERIES_PER_DAY = 30
RECOVERY_BACKOFF = 60  # 1 min between recovery attempts

LOG_DIR = Path.home() / ".aqua-remote" / "logs"
STATE_DIR = Path.home() / ".aqua-remote" / "state"
HEARTBEAT_DIR = Path.home() / ".aqua-remote" / "heartbeats"
PID_DIR = Path.home() / ".aqua-remote" / "pids"
RC_USER_ACTIVITY_FILE = Path("/tmp/rc_user_activity")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ensure_dirs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)


class Logger:
    """Simple file+stdout logger with rotation."""

    def __init__(self, session_name: str):
        _ensure_dirs()
        self.log_file = LOG_DIR / f"{session_name}.log"
        self.max_bytes = 100_000

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {level}: {msg}"
        # Write to file only — stdout is redirected to the same file by cli.py
        # Writing to both causes duplicate lines.
        try:
            with open(self.log_file, "a") as f:
                f.write(line + "\n")
            if self.log_file.stat().st_size > self.max_bytes:
                lines = self.log_file.read_text().splitlines()
                self.log_file.write_text("\n".join(lines[-200:]) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tmux helpers
# ---------------------------------------------------------------------------

def capture_tmux(target: str, lines: int = 80) -> str:
    """Capture tmux pane output."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except Exception:
        return ""


def send_tmux(target: str, keys: str, enter: bool = True):
    """Send keys to a tmux pane.

    If keys is empty and enter=True, sends just Enter (no empty string arg).
    """
    cmd = ["tmux", "send-keys", "-t", target]
    if keys:
        cmd.append(keys)
    if enter:
        cmd.append("Enter")
    subprocess.run(cmd, timeout=5, capture_output=True)


def find_remote_url(text: str) -> str | None:
    """Find RC URL in tmux output.

    Matches Claude Remote Control URLs from anthropic.com or claude.ai domains.
    """
    # Primary: console.anthropic.com/claude-code/remote?...
    urls = re.findall(
        r'https://console\.anthropic\.com/claude-code/remote\?[^\s]+',
        text, re.IGNORECASE,
    )
    if not urls:
        # Fallback: claude.ai remote URLs
        urls = re.findall(
            r'https://claude\.ai/[^\s]*remote[^\s]*',
            text, re.IGNORECASE,
        )
    if not urls:
        # Broad fallback: any anthropic/claude URL with "remote" in path
        urls = re.findall(
            r'https://(?:console\.anthropic\.com|claude\.ai)/[^\s]*',
            text, re.IGNORECASE,
        )
    return urls[-1] if urls else None


def detect_rc_state(content: str) -> str:
    """Detect RC state from the LAST 5 lines of tmux output.

    Only checks the tail to avoid false positives from Claude's own text output
    (e.g. Claude printing 'reconnecting' as part of a conversation about RC).
    The real RC status appears in the Claude Code status bar at the bottom.
    """
    # Use only last 5 lines — the status bar is always at the bottom
    tail = "\n".join(content.strip().split("\n")[-5:])
    if "Remote Control reconnecting" in tail:
        return "reconnecting"
    if re.search(
        r'Remote Control.*connected|Remote Control active|remote session active',
        tail, re.IGNORECASE,
    ):
        return "connected"
    return "unknown"


def is_pilot_busy(content: str) -> bool:
    """Check if the session is actively working.

    Detects:
    - "esc to interrupt" — universal indicator of active Claude processing
    - Braille spinner characters (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) at start of line
    - Common activity words as fallback
    """
    tail = "\n".join(content.strip().split("\n")[-5:])
    # Universal: "esc to interrupt" shown during all active processing
    if "esc to interrupt" in tail:
        return True
    # Braille spinner characters at start of any line
    if re.search(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]', tail, re.MULTILINE):
        return True
    # Fallback: common processing indicators
    if re.search(r"Running|thinking|Working", tail, re.IGNORECASE):
        return True
    return False


def is_user_typing(content: str) -> bool:
    """Check if the user appears to be mid-input.

    Only checks for STRONG indicators of active typing — things that would
    be corrupted by injecting tmux send-keys. Weak indicators (text after ❯)
    are intentionally ignored because they could be stale prompt history or
    a message that was already submitted.

    Detects:
    - "queued messages" — user has pending input waiting to be processed
    """
    tail = "\n".join(content.strip().split("\n")[-5:])
    if "queued messages" in tail:
        return True
    return False


def touch_user_activity():
    """Mark that user/session was recently active. Used to prevent interruption."""
    try:
        RC_USER_ACTIVITY_FILE.write_text(str(int(time.time())))
    except Exception:
        pass


def is_user_recently_active(seconds: int = 60) -> bool:
    """Check if user was active within last N seconds."""
    try:
        if not RC_USER_ACTIVITY_FILE.exists():
            return False
        ts = int(RC_USER_ACTIVITY_FILE.read_text().strip())
        return (int(time.time()) - ts) < seconds
    except Exception:
        return False


def detect_meaningful_change(old: str, new: str) -> bool:
    """Check if tmux content changed meaningfully."""
    def sig_lines(text: str) -> list[str]:
        lines = text.strip().split("\n")[-15:]
        out = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if re.match(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✻\s]*$', s):
                continue
            if re.match(r'^\d{2}:\d{2}:\d{2}\s*$', s):
                continue
            # Skip recovery-induced noise (prevents feedback loop)
            if "Unknown skill" in s:
                continue
            if "Remote Control reconnecting" in s:
                continue
            if "Remote Control connecting" in s:
                continue
            out.append(s)
        return out[-8:]
    return sig_lines(old) != sig_lines(new)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def _cleanup_bridge_pointers(log: Logger):
    """Remove stale bridge-pointer.json files that cause reconnecting loops."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return
    for bp in claude_dir.rglob("bridge-pointer.json"):
        try:
            bp.unlink()
            log.log(f"Removed stale bridge pointer: {bp}")
        except Exception:
            pass


def _dismiss_menu_or_prompt(target: str, content: str, log: Logger):
    """Dismiss RC menu (select Continue) or rating prompts."""
    # RC menu: "Enter to select" with Continue/Disconnect options
    if re.search(r"Enter to select|Continue.*Disconnect|Disconnect this session", content):
        # "Continue" is the default (bottom option) — just press Enter
        log.log("RC menu detected — pressing Enter (Continue)")
        send_tmux(target, "Enter", enter=False)
        time.sleep(3)
        return True

    # Rating prompt: "How is Claude doing"
    if re.search(r"How is Claude doing", content):
        log.log("Rating prompt detected — dismissing")
        send_tmux(target, "0")
        time.sleep(2)
        return True

    return False


def recover_rc(target: str, log: Logger) -> str | None:
    """Attempt to recover RC link. Returns URL or None.

    Recovery strategy:
    1. If "reconnecting" → Ctrl+C + Escape + bridge cleanup → fresh /remote-control
    2. If stuck on menu → dismiss (Enter for Continue)
    3. Send /remote-control → wait for URL → auto-accept Continue menu
    4. Verify session resumed
    """
    content = capture_tmux(target, 30)

    # Check if user was recently active (e.g. typing on phone via RC)
    # Skip this attempt — main loop will retry after CHECK_INTERVAL cycles
    if is_user_recently_active(60):
        log.log("User active <60s ago — postponing recovery 15min (RC protection)")
        return "SKIP_USER_ACTIVE"

    # Check if user is currently typing — never interrupt user input
    if is_user_typing(content):
        log.log("User appears to be typing — postponing recovery")
        return None

    # If reconnecting, clear stale state first
    # NOTE: Claude Code has NO /disconnect command — use Ctrl+C + Escape instead
    if "reconnecting" in detect_rc_state(content):
        log.log("RC reconnecting — clearing stale state with Ctrl+C + bridge cleanup")
        # Re-check user typing before sending keys
        if is_user_typing(capture_tmux(target, 10)):
            log.log("User typing detected before clearing — postponing")
            return None
        # Clean up stale bridge pointer files
        _cleanup_bridge_pointers(log)
        # Cancel reconnecting with Ctrl+C then Escape
        send_tmux(target, "C-c", enter=False)
        time.sleep(3)
        send_tmux(target, "Escape", enter=False)
        time.sleep(3)
        content = capture_tmux(target, 10)
        if "reconnecting" in content.lower():
            log.log("Still reconnecting after Ctrl+C — waiting longer")
            time.sleep(10)

    # If stuck on an old RC menu or rating prompt, dismiss it
    content = capture_tmux(target, 10)
    _dismiss_menu_or_prompt(target, content, log)

    # NOTE: Do NOT skip recovery when pilot is busy — RC must work even during processing.
    # /remote-control queues in terminal buffer and executes when pilot finishes.

    # Final check: user might have started typing during our checks
    if is_user_typing(capture_tmux(target, 10)):
        log.log("User started typing — postponing recovery")
        return None

    # Wait briefly for pilot to be idle (so /remote-control executes immediately
    # instead of queuing). Max 15s wait, then send anyway.
    for wait_attempt in range(3):
        if not is_pilot_busy(capture_tmux(target, 10)):
            break
        log.log(f"Pilot busy — waiting for idle ({(wait_attempt+1)*5}s/15s)")
        time.sleep(5)

    # Send /remote-control
    log.log("Sending /remote-control...")
    send_tmux(target, "/remote-control")

    # Poll for RC URL or menu (max 90s) — handles both immediate and queued execution
    log.log("Waiting for RC to process...")
    queued_logged = False
    for poll in range(18):
        time.sleep(5)
        poll_content = capture_tmux(target, 30)
        # Menu appeared — dismiss it
        if _dismiss_menu_or_prompt(target, poll_content, log):
            time.sleep(3)
            break
        # URL appeared — done
        if find_remote_url(poll_content):
            break
        # RC active in status bar — done
        if "Remote Control active" in poll_content:
            break
        # Command queued on prompt (pilot still busy) — wait patiently
        if re.search(r'❯.*/?remote-control', poll_content):
            if not queued_logged:
                log.log("Command queued on prompt — waiting for pilot to finish...")
                queued_logged = True
            continue

    # Check for URL
    content = capture_tmux(target, 30)
    url = find_remote_url(content)
    if url:
        log.log(f"RC URL found: {url[:60]}...")

    # Handle menu prompts — press Enter to select "Continue" (default)
    _dismiss_menu_or_prompt(target, content, log)

    # Verify session is active (max 60s)
    for attempt in range(12):
        time.sleep(5)
        check = capture_tmux(target, 10)

        # Session is back if busy or on prompt
        if is_pilot_busy(check) or re.search(r"^❯", check, re.MULTILINE):
            log.log(f"Session active after {(attempt + 1) * 5}s")
            break

        # Still on menu? Keep pressing Enter
        if re.search(r"Enter to select|Continue|Disconnect", check):
            log.log("Still on RC menu — pressing Enter")
            send_tmux(target, "Enter", enter=False)

        # Rating prompt? Dismiss
        if re.search(r"How is Claude doing", check):
            send_tmux(target, "0")
    else:
        log.log("Session did not resume after 60s recovery", "WARN")

    return url


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(session_name: str) -> dict:
    state_file = STATE_DIR / f"{session_name}.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text())
    except Exception:
        pass
    return {"last_url": "", "last_recovery_ts": 0, "recovery_count_today": 0, "last_date": ""}


def save_state(session_name: str, state: dict):
    state_file = STATE_DIR / f"{session_name}.json"
    try:
        state_file.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def write_heartbeat(session_name: str):
    hb_file = HEARTBEAT_DIR / f"{session_name}"
    hb_file.write_text(str(int(time.time())))


def check_heartbeat(session_name: str, max_age: int = 300) -> bool:
    """Check if heartbeat is fresh. Returns True if alive."""
    hb_file = HEARTBEAT_DIR / f"{session_name}"
    if not hb_file.exists():
        return False
    try:
        beat = int(hb_file.read_text().strip())
        return (int(time.time()) - beat) < max_age
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _write_pid_file(session_name: str) -> Path:
    """Write PID file and register cleanup."""
    pid_file = PID_DIR / f"{session_name}.pid"
    pid_file.write_text(str(os.getpid()))
    return pid_file


def _remove_pid_file(session_name: str):
    """Remove PID file."""
    pid_file = PID_DIR / f"{session_name}.pid"
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass


def read_pid_file(session_name: str) -> int | None:
    """Read PID from file. Returns PID or None if missing/stale."""
    pid_file = PID_DIR / f"{session_name}.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        # Check if process is actually running
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # Stale PID file — clean up
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def run_monitor(tmux_target: str, session_name: str):
    """Main monitoring loop."""
    _ensure_dirs()
    log = Logger(session_name)
    channel = create_channel()
    state = load_state(session_name)

    # Write PID file
    pid_file = _write_pid_file(session_name)
    atexit.register(_remove_pid_file, session_name)

    # SIGTERM handler for clean shutdown
    def _sigterm_handler(signum, frame):
        log.log("Monitor stopped (SIGTERM)")
        channel.send(
            f"aqua-remote: {session_name} — stopped",
            f"Monitor for <code>{tmux_target}</code> received SIGTERM.",
        )
        _remove_pid_file(session_name)
        # Remove heartbeat so watchdog knows we're gone intentionally
        hb_file = HEARTBEAT_DIR / session_name
        try:
            hb_file.unlink(missing_ok=True)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    log.log(f"=== aqua-remote monitor START === session={session_name} target={tmux_target} pid={os.getpid()}")
    channel.send(
        f"aqua-remote: {session_name} started",
        f"Monitoring tmux session <code>{tmux_target}</code>.\n"
        f"Will send RC links and auto-recover on disconnect.",
    )

    last_content = ""
    last_change_ts = time.time()
    last_heartbeat_log_ts = 0.0
    disconnect_notified = False
    recovery_in_progress = False

    # Immediate check on startup: dismiss any blocking menu/prompt
    # that may have been left by a previous /remote-control invocation
    time.sleep(3)
    startup_content = capture_tmux(tmux_target, 30)
    if startup_content:
        if _dismiss_menu_or_prompt(tmux_target, startup_content, log):
            log.log("Dismissed blocking menu/prompt on startup")
        # Also capture any RC URL visible at startup
        startup_url = find_remote_url(startup_content)
        if startup_url and startup_url != state.get("last_url"):
            state["last_url"] = startup_url
            save_state(session_name, state)
            channel.send(
                f"aqua-remote: {session_name} — RC link",
                f"Session: <code>{tmux_target}</code>\n\n"
                f"<code>{startup_url}</code>\n\n"
                f"Click to connect.",
            )
            log.log(f"Startup RC URL: {startup_url[:60]}...")

    while True:
        try:
            content = capture_tmux(tmux_target, 80)
            if not content:
                time.sleep(CHECK_INTERVAL)
                continue

            now = time.time()
            today = datetime.now().strftime("%Y-%m-%d")

            # Reset daily counter
            if state.get("last_date") != today:
                state["recovery_count_today"] = 0
                state["last_date"] = today
                save_state(session_name, state)

            # Detect meaningful change
            if detect_meaningful_change(last_content, content):
                last_change_ts = now
                # Only mark user activity when session transitions from idle→busy
                # (= user just sent a message). Claude's own output doesn't count.
                was_idle = not is_pilot_busy(last_content) if last_content else False
                now_busy = is_pilot_busy(content)
                if was_idle and now_busy:
                    touch_user_activity()
                if disconnect_notified:
                    disconnect_notified = False
                    log.log("Activity resumed")
                    channel.send(
                        f"aqua-remote: {session_name} reconnected",
                        f"Session <code>{tmux_target}</code> is active again.",
                    )
            last_content = content

            # Dismiss any blocking menu/prompt ASAP — these halt the session
            if _dismiss_menu_or_prompt(tmux_target, content, log):
                # Re-capture after dismiss to get clean state
                time.sleep(3)
                content = capture_tmux(tmux_target, 80)
                last_change_ts = now  # reset idle timer after dismiss

            # Detect RC state and URL
            rc_state = detect_rc_state(content)
            url = find_remote_url(content)
            if url and url != state.get("last_url"):
                state["last_url"] = url
                save_state(session_name, state)
                channel.send(
                    f"aqua-remote: {session_name} — new RC link",
                    f"Session: <code>{tmux_target}</code>\n\n"
                    f"<code>{url}</code>\n\n"
                    f"Click to connect.",
                )
                log.log(f"New RC URL: {url[:60]}...")

            # Check idle / reconnecting / RC dropped
            idle_time = now - last_change_ts
            should_recover = False

            time_since_recovery = now - state.get("last_recovery_ts", 0)
            if rc_state == "reconnecting" and not disconnect_notified and time_since_recovery > 600:
                should_recover = True
                disconnect_notified = True
                log.log("RC reconnecting detected (>10min since last recovery)", "WARN")

            # RC was connected but now it's gone — recover
            # But NOT if /remote-control is already queued on the prompt (waiting for pilot)
            rc_queued = bool(re.search(r'❯.*/?remote-control', content))
            if rc_state == "unknown" and state.get("last_rc_state") == "connected" and time_since_recovery > RECOVERY_BACKOFF and not rc_queued:
                should_recover = True
                # Don't set disconnect_notified — keep retrying until RC is back
                log.log("RC dropped (was connected, now gone) — triggering recovery", "WARN")
            elif rc_queued:
                log.log("RC command queued on prompt — waiting, not re-sending")

            if idle_time > DISCONNECT_THRESHOLD and not disconnect_notified:
                should_recover = True
                disconnect_notified = True
                log.log(f"Idle timeout: {int(idle_time)}s", "WARN")

            # Track RC state transitions
            state["last_rc_state"] = rc_state

            # Recovery
            if should_recover and not recovery_in_progress:
                time_since_last = now - state.get("last_recovery_ts", 0)
                if time_since_last < RECOVERY_BACKOFF:
                    log.log(f"Backoff: {int(time_since_last)}s since last recovery")
                elif state.get("recovery_count_today", 0) >= MAX_RECOVERIES_PER_DAY:
                    log.log("Daily recovery limit reached", "ERROR")
                    channel.send(
                        f"aqua-remote: {session_name} — LIMIT REACHED",
                        f"Session <code>{tmux_target}</code> hit {MAX_RECOVERIES_PER_DAY} "
                        f"recoveries today. Check manually.",
                    )
                # NOTE: pilot busy does NOT block recovery — RC must always work
                elif is_user_typing(content):
                    log.log("User typing — postponing recovery")
                else:
                    count = state.get("recovery_count_today", 0) + 1
                    channel.send(
                        f"aqua-remote: {session_name} — recovering (#{count})",
                        f"Session <code>{tmux_target}</code> idle for {int(idle_time)}s.\n"
                        f"RC state: {rc_state}\n"
                        f"Attempting auto-recovery...",
                    )
                    recovery_in_progress = True
                    new_url = recover_rc(tmux_target, log)
                    recovery_in_progress = False

                    if new_url == "SKIP_USER_ACTIVE":
                        # User active — retry after backoff, don't count as attempt
                        state["last_recovery_ts"] = now
                        disconnect_notified = False  # allow re-trigger next cycle
                        save_state(session_name, state)
                    else:
                        state["last_recovery_ts"] = now
                        state["recovery_count_today"] = count
                        if new_url:
                            state["last_url"] = new_url
                        save_state(session_name, state)

            # Heartbeat
            write_heartbeat(session_name)
            if now - last_heartbeat_log_ts > HEARTBEAT_INTERVAL:
                last_heartbeat_log_ts = now
                log.log(
                    f"Heartbeat: rc={rc_state}, idle={int(idle_time)}s, "
                    f"recoveries={state.get('recovery_count_today', 0)}",
                )

        except KeyboardInterrupt:
            log.log("Monitor stopped by user")
            channel.send(
                f"aqua-remote: {session_name} — stopped",
                f"Monitor for <code>{tmux_target}</code> was manually stopped.",
            )
            break
        except Exception as e:
            log.log(f"Error: {e}", "ERROR")

        time.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="aqua-remote: auto-recover Claude Code Remote Control",
    )
    parser.add_argument(
        "--session", "-s", required=True,
        help="Tmux target (e.g. sol:0, work:2)",
    )
    parser.add_argument(
        "--name", "-n", default="",
        help="Human-readable session name (e.g. pilot, backend). "
             "Defaults to tmux target with : replaced by -.",
    )
    args = parser.parse_args()

    session_name = args.name or args.session.replace(":", "-")
    run_monitor(args.session, session_name)


if __name__ == "__main__":
    main()
