#!/usr/bin/env python3
"""
aqua-remote CLI — manage Remote Control monitoring for Claude Code sessions.

Commands:
    setup    Interactive first-time configuration
    start    Start monitoring a tmux session
    stop     Stop monitoring a session
    status   Show status of all monitored sessions
    test     Send a test notification
    install  Install cron watchdog
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# Add src dir to path
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

from notify import create_channel, load_config, CONFIG_FILE
from monitor import (
    check_heartbeat, HEARTBEAT_DIR, STATE_DIR, LOG_DIR, PID_DIR,
    _ensure_dirs, read_pid_file,
)


def cmd_setup(args):
    """Run interactive setup wizard."""
    from setup import run_setup
    run_setup()


def cmd_start(args):
    """Start monitoring a session."""
    _ensure_dirs()

    if not shutil.which("tmux"):
        print("ERROR: tmux not found. Install it first.")
        sys.exit(1)

    target = args.session
    name = args.name or target.replace(":", "-")

    # Verify tmux target exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", target.split(":")[0]],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"ERROR: tmux session '{target.split(':')[0]}' not found.")
        print("Available sessions:")
        subprocess.run(["tmux", "ls"])
        sys.exit(1)

    # Check if already running (PID file first, pgrep fallback)
    existing_pid = read_pid_file(name)
    if existing_pid:
        print(f"Monitor for '{name}' already running (PID: {existing_pid})")
        if not args.force:
            print("Use --force to restart.")
            sys.exit(1)
        # Kill existing
        os.kill(existing_pid, signal.SIGTERM)
        import time
        time.sleep(2)
    else:
        # Fallback: pgrep for processes without PID file
        result = subprocess.run(
            ["pgrep", "-f", f"monitor.py.*--name.*{name}"],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            print(f"Monitor for '{name}' already running (PID: {', '.join(pids)})")
            if not args.force:
                print("Use --force to restart.")
                sys.exit(1)
            for pid in pids:
                try:
                    os.kill(int(pid.strip()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            import time
            time.sleep(2)

    # Save tmux_target in state for watchdog
    state_file = STATE_DIR / f"{name}.json"
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            pass
    state["tmux_target"] = target
    state_file.write_text(json.dumps(state, indent=2))

    # Start monitor
    log_file = LOG_DIR / f"{name}.log"
    monitor_script = SRC_DIR / "monitor.py"

    if args.foreground:
        print(f"Starting monitor for {target} (name={name}) in foreground...")
        os.execvp(
            sys.executable,
            [sys.executable, str(monitor_script),
             "--session", target, "--name", name],
        )
    else:
        log_fh = open(log_file, "a")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(monitor_script),
                 "--session", target, "--name", name],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        print(f"Monitor started: session={target}, name={name}, PID={proc.pid}")
        print(f"Logs: {log_file}")

        # Also trigger /remote-control in the target session
        if args.rc:
            print(f"Sending /remote-control to {target}...")
            import time
            time.sleep(3)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "/remote-control", "Enter"],
                timeout=5,
            )


def cmd_stop(args):
    """Stop monitoring a session."""
    name = args.name
    stopped = False

    # Try PID file first
    pid = read_pid_file(name)
    if pid:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped monitor PID {pid}")
        stopped = True
    else:
        # Fallback: pgrep
        result = subprocess.run(
            ["pgrep", "-f", f"monitor.py.*--name.*{name}"],
            capture_output=True, text=True,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if pids:
            for p in pids:
                try:
                    os.kill(int(p), signal.SIGTERM)
                    print(f"Stopped monitor PID {p}")
                    stopped = True
                except ProcessLookupError:
                    pass

    if not stopped:
        print(f"No monitor running for '{name}'.")
        return

    # Clean up PID file
    pid_file = PID_DIR / f"{name}.pid"
    if pid_file.exists():
        pid_file.unlink()

    # Clean heartbeat
    hb = HEARTBEAT_DIR / name
    if hb.exists():
        hb.unlink()


def cmd_status(args):
    """Show status of all monitored sessions."""
    _ensure_dirs()
    import time

    now = int(time.time())
    sessions = []

    for hb_file in sorted(HEARTBEAT_DIR.iterdir()):
        name = hb_file.name
        try:
            beat = int(hb_file.read_text().strip())
            age = now - beat
        except Exception:
            age = 999999

        # Get target from state
        state_file = STATE_DIR / f"{name}.json"
        target = "?"
        if state_file.exists():
            try:
                s = json.loads(state_file.read_text())
                target = s.get("tmux_target", "?")
            except Exception:
                pass

        alive = age < 300
        status = "ALIVE" if alive else "DEAD"
        sessions.append((name, target, status, age))

    if not sessions:
        print("No monitored sessions found.")
        print("Start one with: aqua-remote start --session sol:0 --name pilot")
        return

    print(f"{'Name':<15} {'Target':<12} {'Status':<8} {'Heartbeat'}")
    print("-" * 50)
    for name, target, status, age in sessions:
        age_str = f"{age}s ago" if age < 600 else f"{age // 60}m ago"
        print(f"{name:<15} {target:<12} {status:<8} {age_str}")


def cmd_test(args):
    """Send a test notification."""
    cfg = load_config()
    if not cfg:
        print(f"No config found at {CONFIG_FILE}")
        print("Run: aqua-remote setup")
        sys.exit(1)

    channel = create_channel(cfg)
    print(f"Sending test via {cfg.get('channel', 'stdout')}...")
    ok = channel.test()
    if ok:
        print("Success!")
    else:
        print("Failed. Check your config.")
        sys.exit(1)


def cmd_install(args):
    """Install cron watchdog."""
    _ensure_dirs()

    # Copy watchdog to ~/.aqua-remote/
    watchdog_src = SRC_DIR / "watchdog.sh"
    watchdog_dst = Path.home() / ".aqua-remote" / "watchdog.sh"
    shutil.copy2(watchdog_src, watchdog_dst)
    watchdog_dst.chmod(0o755)

    # Check if already in cron
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True,
    )
    existing = result.stdout if result.returncode == 0 else ""

    if "aqua-remote" in existing or "watchdog.sh" in existing:
        print("Watchdog already in crontab.")
        return

    # Add to cron
    new_cron = existing.rstrip() + f"\n*/5 * * * * bash {watchdog_dst}\n"
    proc = subprocess.run(
        ["crontab", "-"], input=new_cron, text=True, capture_output=True,
    )
    if proc.returncode == 0:
        print(f"Watchdog installed in crontab (every 5 min)")
        print(f"Script: {watchdog_dst}")
    else:
        print(f"Failed to install cron: {proc.stderr}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="aqua-remote",
        description="Auto-recover Claude Code Remote Control links",
    )
    subs = parser.add_subparsers(dest="command")

    # setup
    subs.add_parser("setup", help="Interactive first-time configuration")

    # start
    p_start = subs.add_parser("start", help="Start monitoring a session")
    p_start.add_argument("--session", "-s", required=True, help="Tmux target (e.g. sol:0)")
    p_start.add_argument("--name", "-n", default="", help="Session name (e.g. pilot)")
    p_start.add_argument("--force", "-f", action="store_true", help="Restart if already running")
    p_start.add_argument("--foreground", action="store_true", help="Run in foreground")
    p_start.add_argument("--no-rc", dest="rc", action="store_false", default=True,
                          help="Don't auto-send /remote-control")

    # stop
    p_stop = subs.add_parser("stop", help="Stop monitoring a session")
    p_stop.add_argument("--name", "-n", required=True, help="Session name")

    # status
    subs.add_parser("status", help="Show status of all sessions")

    # test
    subs.add_parser("test", help="Send a test notification")

    # install
    subs.add_parser("install", help="Install cron watchdog")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "setup": cmd_setup,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "test": cmd_test,
        "install": cmd_install,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
