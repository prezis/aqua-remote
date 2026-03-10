#!/usr/bin/env python3
"""
aqua-remote monitor — watches a Claude Code tmux session, auto-recovers RC.

Usage:
    python3 monitor.py --session sol:0 --name pilot
    python3 monitor.py --session work:2 --name backend

Features:
- Detects idle sessions (no meaningful output change)
- Detects "RC reconnecting" state
- Auto-recovery: sends /disconnect + /remote-control
- Sends new RC link to configured notification channel
- Heartbeat file for external watchdog
- Session name in all alerts
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from notify import create_channel, load_config, NotifyChannel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHECK_INTERVAL = 30  # seconds between checks
DISCONNECT_THRESHOLD = 900  # 15 min idle = trigger recovery
HEARTBEAT_INTERVAL = 1800  # 30 min between logged heartbeats
MAX_RECOVERIES_PER_DAY = 20
RECOVERY_BACKOFF = 300  # 5 min between recovery attempts

LOG_DIR = Path.home() / ".aqua-remote" / "logs"
STATE_DIR = Path.home() / ".aqua-remote" / "state"
HEARTBEAT_DIR = Path.home() / ".aqua-remote" / "heartbeats"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ensure_dirs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)


class Logger:
    """Simple file+stdout logger with rotation."""

    def __init__(self, session_name: str):
        _ensure_dirs()
        self.log_file = LOG_DIR / f"{session_name}.log"
        self.max_bytes = 100_000

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {level}: {msg}"
        print(line, flush=True)
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
    """Send keys to a tmux pane."""
    cmd = ["tmux", "send-keys", "-t", target, keys]
    if enter:
        cmd.append("Enter")
    subprocess.run(cmd, timeout=5, capture_output=True)


def find_remote_url(text: str) -> str | None:
    """Find RC URL in tmux output."""
    urls = re.findall(r'https://[^\s]+remote[^\s]*', text, re.IGNORECASE)
    if not urls:
        urls = re.findall(
            r'https://(?:console\.anthropic|claude\.ai)[^\s]+',
            text, re.IGNORECASE,
        )
    return urls[-1] if urls else None


def detect_rc_state(content: str) -> str:
    """Detect RC state: 'connected', 'reconnecting', 'unknown'."""
    if "Remote Control reconnecting" in content:
        return "reconnecting"
    if re.search(
        r'Remote Control.*connected|remote session active',
        content, re.IGNORECASE,
    ):
        return "connected"
    return "unknown"


def is_pilot_busy(content: str) -> bool:
    """Check if the session is actively working."""
    tail = "\n".join(content.strip().split("\n")[-5:])
    return bool(re.search(
        r"esc to interrupt|Running|thinking|Working|Channeling|Symbioting|"
        r"Sautéed|Baked|Flowing|Crunched",
        tail,
    ))


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
            out.append(s)
        return out[-8:]
    return sig_lines(old) != sig_lines(new)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def recover_rc(target: str, log: Logger) -> str | None:
    """Attempt to recover RC link. Returns URL or None."""
    content = capture_tmux(target, 30)

    # If reconnecting, disconnect first
    if "reconnecting" in detect_rc_state(content):
        log.log("RC reconnecting — disconnecting first")
        send_tmux(target, "/disconnect")
        time.sleep(5)
        content = capture_tmux(target, 10)
        if "reconnecting" in content.lower():
            send_tmux(target, "C-c", enter=False)
            time.sleep(2)
            send_tmux(target, "/disconnect")
            time.sleep(5)

    # Check if session is on prompt (not busy)
    if is_pilot_busy(capture_tmux(target, 10)):
        log.log("Session busy — postponing recovery")
        return None

    # Send /remote-control
    log.log("Sending /remote-control...")
    send_tmux(target, "/remote-control")
    time.sleep(12)

    # Check for URL
    content = capture_tmux(target, 30)
    url = find_remote_url(content)
    if url:
        log.log(f"RC URL found: {url[:60]}...")

    # Handle menu prompts (Enter to continue)
    if re.search(r"Enter to select|Continue|How is Claude", content):
        send_tmux(target, "")  # just Enter
        time.sleep(3)

    # Verify session is active
    for attempt in range(6):
        time.sleep(5)
        check = capture_tmux(target, 10)
        if is_pilot_busy(check) or re.search(r"^❯", check, re.MULTILINE):
            log.log(f"Session active after {attempt + 1} checks")
            break
    else:
        log.log("Session did not resume after recovery", "WARN")

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

def run_monitor(tmux_target: str, session_name: str):
    """Main monitoring loop."""
    _ensure_dirs()
    log = Logger(session_name)
    channel = create_channel()
    state = load_state(session_name)

    log.log(f"=== aqua-remote monitor START === session={session_name} target={tmux_target}")
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
                if disconnect_notified:
                    disconnect_notified = False
                    log.log("Activity resumed")
                    channel.send(
                        f"aqua-remote: {session_name} reconnected",
                        f"Session <code>{tmux_target}</code> is active again.",
                    )
            last_content = content

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

            # Check idle / reconnecting
            idle_time = now - last_change_ts
            should_recover = False

            if rc_state == "reconnecting" and not disconnect_notified:
                should_recover = True
                disconnect_notified = True
                log.log("RC reconnecting detected", "WARN")

            if idle_time > DISCONNECT_THRESHOLD and not disconnect_notified:
                should_recover = True
                disconnect_notified = True
                log.log(f"Idle timeout: {int(idle_time)}s", "WARN")

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
                elif is_pilot_busy(content):
                    log.log("Session busy — postponing recovery")
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
