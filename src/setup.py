#!/usr/bin/env python3
"""
aqua-remote setup wizard — interactive first-time configuration.

Guides the user through:
1. Checking tmux is available and session exists
2. Choosing notification channel (Telegram / Discord / Email)
3. Testing the connection
4. Saving config to ~/.aqua-remote/config.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from notify import (
    CONFIG_DIR, CONFIG_FILE,
    TelegramChannel, DiscordChannel, EmailChannel, StdoutChannel,
    save_config, load_config,
)

TMUX_HELP = """
╔══════════════════════════════════════════════════════════════╗
║  ERROR: tmux is required but not found!                     ║
║                                                             ║
║  aqua-remote needs tmux to monitor Claude Code sessions.    ║
║                                                             ║
║  Install tmux:                                              ║
║    Ubuntu/Debian:  sudo apt install tmux                    ║
║    macOS:          brew install tmux                        ║
║    Fedora:         sudo dnf install tmux                    ║
║                                                             ║
║  Then start a tmux session:                                 ║
║    tmux new-session -s work                                 ║
║                                                             ║
║  Run Claude Code inside tmux:                               ║
║    claude                                                   ║
║                                                             ║
║  Now you can use aqua-remote to monitor it!                 ║
╚══════════════════════════════════════════════════════════════╝
"""

NOT_IN_TMUX_HELP = """
╔══════════════════════════════════════════════════════════════╗
║  You're not inside a tmux session!                          ║
║                                                             ║
║  aqua-remote monitors Claude Code running INSIDE tmux.      ║
║                                                             ║
║  Option 1: Start a new tmux session                         ║
║    tmux new-session -s work                                 ║
║    claude                          # start Claude Code      ║
║                                                             ║
║  Option 2: Move current shell into tmux                     ║
║    1. Start tmux:     tmux new-session -s work              ║
║    2. Open Claude:    claude                                ║
║    3. In another tmux window, run aqua-remote setup         ║
║                                                             ║
║  Quick tmux cheatsheet:                                     ║
║    Ctrl+B, C       = new window                             ║
║    Ctrl+B, N       = next window                            ║
║    Ctrl+B, D       = detach (session keeps running)         ║
║    tmux attach     = re-attach to running session           ║
║    tmux ls         = list sessions                          ║
╚══════════════════════════════════════════════════════════════╝
"""


def check_tmux() -> bool:
    """Check if tmux is installed and accessible."""
    if not shutil.which("tmux"):
        print(TMUX_HELP)
        return False

    # Check if any tmux sessions exist
    result = subprocess.run(
        ["tmux", "ls"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(NOT_IN_TMUX_HELP)
        print("No tmux sessions found. Start one first:")
        print("  tmux new-session -s work")
        print("  claude")
        return False

    print(f"tmux sessions found:\n{result.stdout}")
    return True


def list_tmux_windows() -> list[str]:
    """List all tmux windows as session:window targets."""
    result = subprocess.run(
        ["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_index} #{window_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def setup_telegram() -> dict:
    """Interactive Telegram setup."""
    print("\n--- Telegram Setup ---")
    print()
    print("1. Open Telegram and search for @BotFather")
    print("2. Send /newbot and follow the instructions")
    print("3. Copy the bot token (looks like: 123456789:ABCdef...)")
    print()
    bot_token = input("Bot token: ").strip()
    if not bot_token:
        print("Cancelled.")
        return {}

    print()
    print("4. Create a channel/group or use an existing chat")
    print("5. Add your bot to the chat")
    print("6. Send a message in the chat, then visit:")
    print(f"   https://api.telegram.org/bot{bot_token}/getUpdates")
    print("7. Find your chat_id in the response")
    print()
    print("   For groups/channels it starts with - (e.g. -1001234567890)")
    print("   For DMs it's a positive number (e.g. 123456789)")
    print()
    chat_id = input("Chat ID: ").strip()
    if not chat_id:
        print("Cancelled.")
        return {}

    # Test
    print("\nTesting connection...")
    ch = TelegramChannel(bot_token, chat_id)
    if ch.test():
        print("Success! Check your Telegram.")
        return {
            "channel": "telegram",
            "telegram_bot_token": bot_token,
            "telegram_chat_id": chat_id,
        }
    else:
        print("Failed to send test message. Check token and chat_id.")
        retry = input("Try again? [y/N]: ").strip().lower()
        if retry == "y":
            return setup_telegram()
        return {}


def setup_discord() -> dict:
    """Interactive Discord setup."""
    print("\n--- Discord Setup ---")
    print()
    print("1. In your Discord server, go to Channel Settings > Integrations > Webhooks")
    print("2. Click 'New Webhook'")
    print("3. Name it (e.g. 'aqua-remote')")
    print("4. Copy the webhook URL")
    print()
    webhook_url = input("Webhook URL: ").strip()
    if not webhook_url:
        print("Cancelled.")
        return {}

    print("\nTesting connection...")
    ch = DiscordChannel(webhook_url)
    if ch.test():
        print("Success! Check your Discord channel.")
        return {
            "channel": "discord",
            "discord_webhook_url": webhook_url,
        }
    else:
        print("Failed. Check the webhook URL.")
        return {}


def setup_email() -> dict:
    """Interactive email setup."""
    print("\n--- Email Setup ---")
    print()
    print("You'll need SMTP credentials. For Gmail:")
    print("  1. Enable 2FA on your Google account")
    print("  2. Go to: myaccount.google.com/apppasswords")
    print("  3. Create an App Password for 'Mail'")
    print()
    smtp_host = input("SMTP host [smtp.gmail.com]: ").strip() or "smtp.gmail.com"
    smtp_port = int(input("SMTP port [587]: ").strip() or "587")
    username = input("SMTP username (email): ").strip()
    password = input("SMTP password (app password): ").strip()
    from_addr = input(f"From address [{username}]: ").strip() or username
    to_addr = input("Send alerts to (email): ").strip()

    if not all([username, password, to_addr]):
        print("Cancelled.")
        return {}

    print("\nTesting connection...")
    ch = EmailChannel(smtp_host, smtp_port, username, password, from_addr, to_addr)
    if ch.test():
        print(f"Success! Check {to_addr} inbox.")
        return {
            "channel": "email",
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_username": username,
            "smtp_password": password,
            "email_from": from_addr,
            "email_to": to_addr,
            "smtp_tls": True,
        }
    else:
        print("Failed. Check SMTP credentials.")
        return {}


def run_setup():
    """Run the interactive setup wizard."""
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║        aqua-remote — Setup Wizard               ║")
    print("║                                                  ║")
    print("║  Auto-recover Claude Code Remote Control links   ║")
    print("║  and get notified on your phone/desktop.         ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # Step 1: Check tmux
    if not check_tmux():
        sys.exit(1)

    # Step 2: Check existing config
    existing = load_config()
    if existing:
        print(f"\nExisting config found: channel={existing.get('channel', '?')}")
        overwrite = input("Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("Keeping existing config.")
            return

    # Step 3: Choose notification channel
    print("\nWhere should aqua-remote send alerts?")
    print("  1. Telegram (recommended — works great on mobile)")
    print("  2. Discord (webhook to any channel)")
    print("  3. Email (SMTP)")
    print("  4. stdout only (no external notifications)")
    print()
    choice = input("Choice [1]: ").strip() or "1"

    cfg = {}
    if choice == "1":
        cfg = setup_telegram()
    elif choice == "2":
        cfg = setup_discord()
    elif choice == "3":
        cfg = setup_email()
    elif choice == "4":
        cfg = {"channel": "stdout"}
        print("OK — alerts will only appear in the terminal.")
    else:
        print(f"Unknown choice: {choice}")
        sys.exit(1)

    if not cfg:
        print("\nSetup cancelled.")
        sys.exit(1)

    # Step 4: Save
    save_config(cfg)
    print(f"\nConfig saved to {CONFIG_FILE}")
    print(f"  Channel: {cfg.get('channel')}")

    # Step 5: Show next steps
    windows = list_tmux_windows()
    print("\n--- Next Steps ---")
    print()
    if windows:
        print("Your tmux windows:")
        for w in windows:
            print(f"  {w}")
        print()
    print("To start monitoring a session:")
    print("  aqua-remote start --session sol:0 --name pilot")
    print()
    print("Or use the Claude Code skill:")
    print("  /aqua-remote")
    print()


if __name__ == "__main__":
    run_setup()
