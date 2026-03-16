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
import fcntl
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

CHECK_INTERVAL = 30  # 30s between checks (was 900s — caused watchdog to kill us)
DISCONNECT_THRESHOLD = 900  # 15 min idle = trigger recovery
HEARTBEAT_INTERVAL = 1800  # 30 min between logged heartbeats
MAX_RECOVERIES_PER_DAY = 30
RECOVERY_BACKOFF = 900  # 15 min between recovery attempts

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


def send_tmux_hex(target: str, text: str, enter: bool = True, slow: bool = False):
    """Send text to tmux pane as hex bytes via -H flag.

    Bypasses bracketed paste mode / autocomplete ghost text issues.
    Each character is sent as its hex byte value. If enter=True,
    appends 0x0d (carriage return) to execute the command.

    If slow=True, sends each character individually with a 150ms delay.
    This avoids ghost text corruption from Claude Code's autocomplete
    intercepting the input when all bytes arrive at once.
    """
    hex_bytes = [format(b, "02x") for b in text.encode("utf-8")]
    if slow:
        # Send each byte individually with delay to avoid ghost text
        for hb in hex_bytes:
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "-H", hb],
                timeout=5, capture_output=True,
            )
            time.sleep(0.15)
        if enter:
            time.sleep(0.3)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "-H", "0d"],
                timeout=5, capture_output=True,
            )
    else:
        if enter:
            hex_bytes.append("0d")  # carriage return = Enter
        cmd = ["tmux", "send-keys", "-t", target, "-H"] + hex_bytes
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
    - "esc to interrupt" — but ONLY when it's NOT on the status bar line.
      The status bar always shows "esc to interrupt" even when idle.
      When Claude is actively processing, it appears on a separate line
      WITHOUT "Remote Control" or model info on the same line.
    - Braille spinner characters (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) at start of line
    - "Misting/Whirring/Cooked" — Claude processing indicators
    """
    lines = content.strip().split("\n")
    tail = "\n".join(lines[-5:])

    # Check "esc to interrupt" — but exclude the status bar line.
    # Status bar has "esc to interrupt" + other info (RC state, model, time)
    # on the SAME line. Active processing shows it alone or with just a spinner.
    for line in lines[-5:]:
        stripped = line.strip()
        if "esc to interrupt" in stripped:
            # Status bar line contains RC info, model name, or timestamps
            is_status_bar = bool(re.search(
                r'Remote Control|claude|model|gemini|\d{2}:\d{2}|\d{2}-\w{3}-\d{2}',
                stripped, re.IGNORECASE,
            ))
            if not is_status_bar:
                return True
            # else: it's just the status bar — not busy

    # Braille spinner characters at start of any line
    if re.search(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]', tail, re.MULTILINE):
        return True
    # Active processing indicators (shown during tool execution)
    if re.search(r'Misting|Whirring|Cooked for', tail):
        return True
    return False


def is_user_typing(content: str) -> bool:
    """Check if the user appears to be mid-input.

    Detects:
    - "queued messages" — user has pending input waiting to be processed
    - Text after the last ❯ prompt — user is typing a command/prompt
      (only checks the LAST line containing ❯ to avoid stale prompt matches)
    """
    tail_lines = content.strip().split("\n")[-5:]
    tail = "\n".join(tail_lines)
    if "queued messages" in tail:
        return True

    # Check if user has text after the LAST ❯ prompt (means they're mid-input)
    # Only look at last 3 lines, and only the LAST line that has ❯
    last_3 = content.strip().split("\n")[-3:]
    last_prompt_line = None
    for line in reversed(last_3):
        if "\u276f" in line:  # ❯ character
            last_prompt_line = line
            break
    if last_prompt_line:
        # Extract text after ❯ — if non-empty and not just whitespace, user is typing
        match = re.search(r'\u276f\s*(.+)', last_prompt_line)
        if match:
            text_after = match.group(1).strip()
            # /remote-control on prompt = aqua-remote typed it, NOT user
            if text_after and not re.match(r'^/?remote-control$', text_after):
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


def _dismiss_menu_or_prompt(target: str, content: str, log: Logger, force_disconnect: bool = False):
    """Dismiss RC menu (select Continue) or rating prompts.

    If force_disconnect=True AND RC is in "reconnecting" state, select Disconnect
    instead of Continue to break the reconnecting loop.
    """
    # RC menu: "Enter to select" with Continue/Disconnect options
    # ONLY check last 5 lines — "Continue" appears in Claude's text output too (false positive!)
    menu_area = "\n".join(content.strip().split("\n")[-5:])
    if re.search(r"Enter to select|Continue.*Disconnect|Disconnect this session", menu_area):
        rc_reconnecting = "Remote Control reconnecting" in menu_area
        if force_disconnect or rc_reconnecting:
            # Select Disconnect (top option) — Up Up Enter
            log.log("RC menu detected — selecting DISCONNECT (reconnecting state)")
            send_tmux(target, "Up", enter=False)
            time.sleep(0.3)
            send_tmux(target, "Up", enter=False)
            time.sleep(0.3)
            send_tmux(target, "", enter=True)
            time.sleep(5)
            # Clean bridge pointers after disconnect
            _cleanup_bridge_pointers(log)
            time.sleep(3)
            # Now send fresh /remote-control to get new link (slow to avoid ghost text)
            log.log("Sending fresh /remote-control after disconnect...")
            send_tmux(target, "Escape", enter=False)
            time.sleep(0.5)
            send_tmux(target, "C-u", enter=False)
            time.sleep(1)
            send_tmux_hex(target, "/remote-control", enter=True, slow=True)
            time.sleep(8)
            # Check for new menu (Continue on fresh connection) and accept it
            fresh_content = capture_tmux(target, 20)
            fresh_tail = "\n".join(fresh_content.strip().split("\n")[-5:])
            if re.search(r"Enter to select|Continue.*Disconnect", fresh_tail):
                log.log("Fresh RC menu — pressing Enter (Continue)")
                send_tmux(target, "", enter=True)
                time.sleep(3)
        else:
            # Normal case: "Continue" is the default — just press Enter
            log.log("RC menu detected — pressing Enter (Continue)")
            send_tmux(target, "", enter=True)
            time.sleep(3)
        return True

    # Rating prompt: "How is Claude doing"
    # Only check LAST 5 lines to avoid matching old scrollback text
    last_lines = "\n".join(content.strip().split("\n")[-5:])
    if re.search(r"How is Claude doing", last_lines):
        last_dismiss = getattr(_dismiss_menu_or_prompt, '_last_rating_dismiss', 0)
        if time.time() - last_dismiss < 120:
            return False  # cooldown active, skip
        log.log("Rating prompt detected (last 5 lines) — pressing Escape")
        send_tmux(target, "Escape", enter=False)
        time.sleep(2)
        _dismiss_menu_or_prompt._last_rating_dismiss = time.time()
        return True

    return False


def recover_rc_hard(target: str, log: Logger) -> str | None:
    """Hard RC reset: disconnect via menu, wait, then fresh /remote-control.

    Used when soft recovery (Ctrl+C + Escape) fails repeatedly for "reconnecting".
    Strategy:
    1. Send /remote-control to get the menu
    2. Navigate UP to "Disconnect this session" and press Enter
    3. Wait for disconnect to complete
    4. Clean bridge pointers
    5. Send fresh /remote-control
    """
    log.log("=== HARD RC RESET ===")

    # Step 0: Wait for pilot to be idle before sending any keys.
    # If pilot is busy (processing), /remote-control goes into input buffer
    # as text instead of being executed as CLI command.
    for idle_wait in range(12):  # max 60s
        idle_check = capture_tmux(target, 10)
        if not is_pilot_busy(idle_check):
            break
        if idle_wait == 0:
            log.log("Waiting for pilot idle before hard reset...")
        time.sleep(5)
    else:
        log.log("Pilot still busy after 60s — proceeding anyway", "WARN")

    # Step 1: Clean bridge pointers first
    _cleanup_bridge_pointers(log)

    # Step 2: Try Ctrl+C to cancel any pending operation
    send_tmux(target, "C-c", enter=False)
    time.sleep(2)
    send_tmux(target, "Escape", enter=False)
    time.sleep(2)

    # Step 3: Send /remote-control to get the menu (slow mode avoids ghost text)
    send_tmux(target, "Escape", enter=False)
    time.sleep(0.5)
    send_tmux(target, "C-u", enter=False)
    time.sleep(1)
    send_tmux_hex(target, "/remote-control", enter=True, slow=True)
    log.log("Hard reset: sent /remote-control for disconnect menu (slow)")

    # Step 4: Wait for menu to appear (max 30s)
    menu_found = False
    for i in range(6):
        time.sleep(5)
        menu_content = capture_tmux(target, 20)
        menu_tail_hard = "\n".join(menu_content.strip().split("\n")[-5:])
        if re.search(r"Enter to select|Continue.*Disconnect|Disconnect this session", menu_tail_hard):
            menu_found = True
            break
        # URL appeared directly (no menu) — RC might have reconnected
        if find_remote_url(menu_content):
            log.log("Hard reset: URL appeared without menu — RC may have self-recovered")
            return find_remote_url(menu_content)

    if menu_found:
        # Step 5: Navigate to "Disconnect this session" — it's the top option, use Up arrow
        log.log("Hard reset: RC menu found — selecting Disconnect (Up + Enter)")
        send_tmux(target, "Up", enter=False)
        time.sleep(0.5)
        send_tmux(target, "Up", enter=False)
        time.sleep(0.5)
        send_tmux(target, "", enter=True)  # Enter to confirm Disconnect
        time.sleep(5)

        # Check if disconnect worked
        dc_content = capture_tmux(target, 10)
        if "reconnecting" not in dc_content.lower():
            log.log("Hard reset: Disconnect successful")
        else:
            log.log("Hard reset: Still reconnecting after disconnect attempt", "WARN")
            # Try once more: Ctrl+C aggressively
            send_tmux(target, "C-c", enter=False)
            time.sleep(3)
            send_tmux(target, "C-c", enter=False)
            time.sleep(5)
    else:
        log.log("Hard reset: No RC menu appeared — trying Ctrl+C x2 + bridge cleanup", "WARN")
        send_tmux(target, "C-c", enter=False)
        time.sleep(2)
        send_tmux(target, "C-c", enter=False)
        time.sleep(5)
        _cleanup_bridge_pointers(log)
        time.sleep(5)

    # Step 6: Wait for session to stabilize
    log.log("Hard reset: Waiting 10s for session to stabilize...")
    time.sleep(10)

    # Step 7: Send fresh /remote-control (slow mode to avoid ghost text)
    send_tmux(target, "Escape", enter=False)
    time.sleep(0.5)
    send_tmux(target, "C-u", enter=False)
    time.sleep(1)
    send_tmux_hex(target, "/remote-control", enter=True, slow=True)
    log.log("Hard reset: Sent fresh /remote-control (slow)")

    # Step 8: Wait for URL or menu (max 60s)
    for poll in range(12):
        time.sleep(5)
        poll_content = capture_tmux(target, 30)
        if _dismiss_menu_or_prompt(target, poll_content, log):
            time.sleep(3)
            break
        url = find_remote_url(poll_content)
        if url:
            log.log(f"Hard reset: RC URL found: {url[:60]}...")
            return url
        if "Remote Control active" in poll_content:
            break

    # Final URL check
    content = capture_tmux(target, 30)
    url = find_remote_url(content)
    _dismiss_menu_or_prompt(target, content, log)

    if url:
        log.log(f"Hard reset: SUCCESS — {url[:60]}...")
    else:
        log.log("Hard reset: FAILED — no URL found", "ERROR")

    return url


def recover_rc(target: str, log: Logger, hard: bool = False) -> str | None:
    """Attempt to recover RC link. Returns URL or None.

    Recovery strategy:
    1. If hard=True → use recover_rc_hard() (disconnect + reconnect)
    2. If "reconnecting" → Ctrl+C + Escape + bridge cleanup → fresh /remote-control
    3. If stuck on menu → dismiss (Enter for Continue)
    4. Send /remote-control → wait for URL → auto-accept Continue menu
    5. Verify session resumed
    """
    content = capture_tmux(target, 30)

    # Check if user was recently active (e.g. typing on phone via RC)
    # Skip this attempt — main loop will retry after CHECK_INTERVAL cycles
    if is_user_recently_active(60):
        log.log("User active <60s ago — will retry next cycle (RC protection)")
        return "SKIP_USER_ACTIVE"

    # Check if user is currently typing — never interrupt user input
    if is_user_typing(content):
        log.log("User appears to be typing — postponing recovery")
        return None

    # Hard reset path — for persistent reconnecting failures
    if hard:
        return recover_rc_hard(target, log)

    # If reconnecting, use disconnect+reconnect strategy (Ctrl+C alone doesn't work!)
    if "reconnecting" in detect_rc_state(content):
        log.log("RC reconnecting — using disconnect+reconnect strategy")
        if is_user_typing(capture_tmux(target, 10)):
            log.log("User typing detected before clearing — postponing")
            return None
        # Wait for pilot idle before sending keys
        for _iw in range(12):  # max 60s
            if not is_pilot_busy(capture_tmux(target, 10)):
                break
            if _iw == 0:
                log.log("Waiting for pilot idle before disconnect...")
            time.sleep(5)
        _cleanup_bridge_pointers(log)
        # Send /remote-control to get menu with Disconnect option (slow to avoid ghost text)
        send_tmux(target, "Escape", enter=False)
        time.sleep(0.5)
        send_tmux(target, "C-u", enter=False)
        time.sleep(1)
        send_tmux_hex(target, "/remote-control", enter=True, slow=True)
        log.log("Sent /remote-control to get disconnect menu (slow)")
        menu_found = False
        for i in range(6):
            time.sleep(5)
            menu_content = capture_tmux(target, 20)
            menu_tail = "\n".join(menu_content.strip().split("\n")[-5:])
            if re.search(r"Enter to select|Continue.*Disconnect|Disconnect this session", menu_tail):
                menu_found = True
                break
        if menu_found:
            log.log("RC menu found — selecting Disconnect (Up+Up+Enter)")
            send_tmux(target, "Up", enter=False)
            time.sleep(0.3)
            send_tmux(target, "Up", enter=False)
            time.sleep(0.3)
            send_tmux(target, "", enter=True)
            time.sleep(5)
            _cleanup_bridge_pointers(log)
            log.log("Disconnected — waiting 5s before fresh /remote-control")
            time.sleep(5)
        else:
            log.log("No menu appeared — Ctrl+C fallback", "WARN")
            send_tmux(target, "C-c", enter=False)
            time.sleep(3)
            send_tmux(target, "Escape", enter=False)
            time.sleep(5)

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

    # Send /remote-control using slow hex bytes to bypass ghost text corruption
    log.log("Sending /remote-control (slow hex mode)...")
    send_tmux(target, "Escape", enter=False)  # clear any autocomplete state
    time.sleep(0.5)
    send_tmux(target, "C-u", enter=False)     # clear current line
    time.sleep(1)
    send_tmux_hex(target, "/remote-control", enter=True, slow=True)

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
        # Command queued on prompt (pilot still busy or ghost text) — send extra Enter
        # Only check last 3 lines to avoid matching scrollback
        poll_prompt = "\n".join(poll_content.strip().split("\n")[-3:])
        if re.search(r'❯.*/?remote-control', poll_prompt):
            if not queued_logged:
                log.log("Command queued on prompt — sending extra Enter to execute...")
                # Ghost text fix: send Enter via hex to force execution
                send_tmux_hex(target, "", enter=True)
                queued_logged = True
            continue

    # Check for URL
    content = capture_tmux(target, 30)
    url = find_remote_url(content)
    if url:
        log.log(f"RC URL found: {url[:60]}...")

    # Handle menu prompts — press Enter to select "Continue" (default)
    _dismiss_menu_or_prompt(target, content, log)

    # Verify RC is active (poll for "Remote Control active" in status bar, max 30s)
    rc_verified = False
    for verify_poll in range(6):
        time.sleep(5)
        verify_content = capture_tmux(target, 10)
        if "Remote Control active" in verify_content or re.search(
            r'Remote Control.*connected|remote session active',
            verify_content, re.IGNORECASE,
        ):
            rc_verified = True
            log.log(f"RC verified active after {(verify_poll + 1) * 5}s")
            break
        # Dismiss any menu that appeared
        _dismiss_menu_or_prompt(target, verify_content, log)
    if not rc_verified:
        log.log("RC not verified active after 30s — session may need manual check", "WARN")

    # Verify session is active (max 60s)
    for attempt in range(12):
        time.sleep(5)
        check = capture_tmux(target, 10)

        # Session is back if busy or on prompt
        if is_pilot_busy(check) or re.search(r"^❯", check, re.MULTILINE):
            log.log(f"Session active after {(attempt + 1) * 5}s")
            break

        # Still on menu? Keep pressing Enter (only check last 5 lines)
        check_tail = "\n".join(check.strip().split("\n")[-5:])
        if re.search(r"Enter to select|Continue.*Disconnect|Disconnect this session", check_tail):
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
    return {
        "last_url": "", "last_recovery_ts": 0, "recovery_count_today": 0,
        "last_date": "", "consecutive_reconnecting_fails": 0,
        "limit_reached_notified": False,
    }


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

def _acquire_singleton_lock(session_name: str) -> int:
    """Acquire an exclusive flock on the lock file. Dies if another instance holds it.

    This is the ONLY reliable way to prevent duplicate monitors. PID files can
    go stale; pgrep can race. flock is atomic and kernel-enforced.

    Strategy:
    1. Try flock(LOCK_NB) — if it succeeds, we're the only one.
    2. If flock fails, another monitor holds it → exit immediately.
    3. After acquiring lock, kill any orphan processes (zombies without lock).

    Returns the lock fd (must be kept open for the lifetime of the process).
    """
    _ensure_dirs()
    lock_path = PID_DIR / f"{session_name}.lock"

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another instance holds the lock — exit immediately
        print(f"FATAL: Another monitor for '{session_name}' is already running (lock held). Exiting.")
        os.close(fd)
        sys.exit(1)

    # Lock acquired — we're the singleton. Kill any orphan processes
    # (e.g. monitors started before flock was introduced, or zombies).
    _kill_existing_monitors(session_name)

    # Write our PID into the lock file for diagnostics
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _kill_existing_monitors(session_name: str):
    """Kill any other monitor.py processes for this session name via pgrep.

    Prevents accumulation of orphan monitors that were started without
    going through the CLI (e.g. by Claude Code spawning subprocesses).
    """
    my_pid = os.getpid()
    my_ppid = os.getppid()
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"monitor.py.*--name {session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if not result.stdout.strip():
            return
        pids_to_kill = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            # Never kill ourselves or our parent
            if pid in (my_pid, my_ppid):
                continue
            pids_to_kill.append(pid)

        if not pids_to_kill:
            return

        for pid in pids_to_kill:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(2)
        # Force-kill survivors
        for pid in pids_to_kill:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


def _write_pid_file(session_name: str) -> Path:
    """Write PID file for status/stop commands."""
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

    # SINGLETON: acquire exclusive flock BEFORE anything else.
    # If another monitor is running, this exits immediately.
    lock_fd = _acquire_singleton_lock(session_name)

    log = Logger(session_name)
    channel = create_channel()
    state = load_state(session_name)

    # Write PID file (for status/stop commands)
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
        # Do NOT delete heartbeat — watchdog will see it's stale and restart us
        # Deleting it caused immediate "ZDECHŁ" alert + restart loop
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
    needs_recovery = False  # persistent flag: RC is down, keep retrying

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
        # If RC is reconnecting on startup, trigger immediate hard recovery
        startup_rc = detect_rc_state(startup_content)
        if startup_rc == "reconnecting":
            log.log("RC reconnecting on startup — triggering immediate hard recovery")
            needs_recovery = True
            new_url = recover_rc(tmux_target, log, hard=True)
            if new_url and new_url != "SKIP_USER_ACTIVE":
                state["last_url"] = new_url
                state["last_recovery_ts"] = time.time()
                state["consecutive_reconnecting_fails"] = 0
                count = state.get("recovery_count_today", 0) + 1
                state["recovery_count_today"] = count
                save_state(session_name, state)
                channel.send(
                    f"aqua-remote: {session_name} — RC recovered (startup hard reset)",
                    f"<code>{new_url}</code>\n\nClick to connect.",
                )
                log.log(f"Startup recovery SUCCESS: {new_url[:60]}...")
                needs_recovery = False
            else:
                state["last_recovery_ts"] = time.time()
                state["recovery_count_today"] = state.get("recovery_count_today", 0) + 1
                save_state(session_name, state)
                log.log("Startup recovery did not produce URL — will retry in main loop", "WARN")

    while True:
        try:
            # Write heartbeat FIRST — so watchdog knows we're alive
            # even if the rest of the loop takes time
            write_heartbeat(session_name)

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
                state["limit_reached_notified"] = False
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
            last_content = content

            # Detect RC state first (needed for menu dismiss logic)
            rc_state = detect_rc_state(content)

            # Dismiss any blocking menu/prompt ASAP — but NEVER when user is typing
            if is_user_typing(content):
                pass  # User is mid-input — don't send any keys
            elif _dismiss_menu_or_prompt(tmux_target, content, log,
                                          force_disconnect=(rc_state == "reconnecting")):
                # Re-capture after dismiss to get clean state
                time.sleep(3)
                content = capture_tmux(tmux_target, 80)
                last_change_ts = now  # reset idle timer after dismiss
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

            # Clear needs_recovery when RC comes back
            if rc_state == "connected" and needs_recovery:
                needs_recovery = False
                state["consecutive_reconnecting_fails"] = 0
                state["limit_reached_notified"] = False
                save_state(session_name, state)
                log.log("RC is back — clearing needs_recovery + reconnecting fail counter")

            # Check for queued /remote-control on prompt (last 3 lines only)
            prompt_area = "\n".join(content.strip().split("\n")[-3:])
            rc_queued = bool(re.search(r'❯.*/?remote-control', prompt_area))

            if rc_queued:
                # Command already on prompt — don't send another, just wait
                pass
            elif rc_state == "reconnecting" and time_since_recovery > 600:
                should_recover = True
                needs_recovery = True
                log.log("RC reconnecting detected (>10min since last recovery)", "WARN")
            elif rc_state == "unknown" and state.get("last_rc_state") == "connected" and time_since_recovery > RECOVERY_BACKOFF:
                # RC just dropped (was connected, now gone)
                should_recover = True
                needs_recovery = True
                log.log("RC dropped (was connected, now gone) — triggering recovery", "WARN")
            elif needs_recovery and rc_state != "connected" and time_since_recovery > RECOVERY_BACKOFF:
                # RC still down from a previous drop — keep retrying
                should_recover = True
                log.log(f"RC still down — retrying recovery (last attempt {int(time_since_recovery)}s ago)", "WARN")
            elif idle_time > DISCONNECT_THRESHOLD and not disconnect_notified and rc_state != "connected":
                # Idle timeout — ONLY when RC is NOT connected
                should_recover = True
                needs_recovery = True
                disconnect_notified = True
                log.log(f"Idle timeout: {int(idle_time)}s, rc={rc_state}", "WARN")

            # Track RC state transitions
            state["last_rc_state"] = rc_state

            # Recovery
            if should_recover and not recovery_in_progress:
                time_since_last = now - state.get("last_recovery_ts", 0)
                consec_recon_fails = state.get("consecutive_reconnecting_fails", 0)

                if time_since_last < RECOVERY_BACKOFF:
                    log.log(f"Backoff: {int(time_since_last)}s since last recovery")
                elif state.get("recovery_count_today", 0) >= MAX_RECOVERIES_PER_DAY:
                    # Daily limit reached — but if RC is STILL reconnecting, do ONE hard reset
                    if rc_state == "reconnecting" and not state.get("limit_reached_notified"):
                        state["limit_reached_notified"] = True
                        save_state(session_name, state)
                        log.log("Daily limit reached but RC still reconnecting — trying HARD RESET", "WARN")
                        channel.send(
                            f"aqua-remote: {session_name} — LIMIT REACHED, trying hard reset",
                            f"Session <code>{tmux_target}</code> hit {MAX_RECOVERIES_PER_DAY} "
                            f"recoveries today. Attempting one hard disconnect+reconnect.",
                        )
                        recovery_in_progress = True
                        new_url = recover_rc(tmux_target, log, hard=True)
                        recovery_in_progress = False
                        state["last_recovery_ts"] = now
                        if new_url and new_url != "SKIP_USER_ACTIVE":
                            state["last_url"] = new_url
                            state["consecutive_reconnecting_fails"] = 0
                            state["limit_reached_notified"] = False
                            save_state(session_name, state)
                            channel.send(
                                f"aqua-remote: {session_name} — HARD RESET SUCCESS",
                                f"<code>{new_url}</code>\n\nRC recovered after hard reset!",
                            )
                        else:
                            save_state(session_name, state)
                            channel.send(
                                f"aqua-remote: {session_name} — HARD RESET FAILED",
                                f"Session <code>{tmux_target}</code> needs manual intervention.",
                            )
                    # Don't spam — only log once per 5 minutes when limit is reached
                    elif not state.get("limit_reached_notified"):
                        state["limit_reached_notified"] = True
                        save_state(session_name, state)
                        log.log("Daily recovery limit reached", "ERROR")
                        channel.send(
                            f"aqua-remote: {session_name} — LIMIT REACHED",
                            f"Session <code>{tmux_target}</code> hit {MAX_RECOVERIES_PER_DAY} "
                            f"recoveries today. Check manually.",
                        )
                    # else: already notified, stay silent
                # NOTE: pilot busy does NOT block recovery — RC must always work
                elif is_user_typing(content):
                    log.log("User typing — postponing recovery")
                else:
                    count = state.get("recovery_count_today", 0) + 1
                    # If RC is stuck reconnecting, ALWAYS use hard recovery
                    # (disconnect first, then fresh connect). Soft recovery
                    # doesn't work for reconnecting — learned the hard way.
                    use_hard = (rc_state == "reconnecting")
                    mode = "HARD" if use_hard else "soft"
                    log.log(f"Starting {mode} recovery #{count} (idle={int(idle_time)}s, rc={rc_state}, consec_recon_fails={consec_recon_fails})")
                    recovery_in_progress = True
                    new_url = recover_rc(tmux_target, log, hard=use_hard)
                    recovery_in_progress = False

                    if new_url == "SKIP_USER_ACTIVE":
                        # User active — retry next cycle (don't set recovery_ts,
                        # so backoff doesn't block). needs_recovery stays True.
                        pass
                    else:
                        state["last_recovery_ts"] = now
                        state["recovery_count_today"] = count
                        if new_url:
                            state["last_url"] = new_url
                            state["consecutive_reconnecting_fails"] = 0
                        elif rc_state == "reconnecting":
                            state["consecutive_reconnecting_fails"] = consec_recon_fails + 1
                        save_state(session_name, state)
                        # Single notification with result + link (no spam)
                        if new_url:
                            channel.send(
                                f"aqua-remote: {session_name} — RC recovered ({mode})",
                                f"<code>{new_url}</code>\n\n"
                                f"Click to connect. (recovery #{count})",
                            )
                        else:
                            channel.send(
                                f"aqua-remote: {session_name} — recovery failed ({mode})",
                                f"Session <code>{tmux_target}</code> recovery #{count} "
                                f"did not produce a new RC link.\n"
                                f"RC state: {rc_state}, consecutive_fails: {consec_recon_fails + 1}",
                            )

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
