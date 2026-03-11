# aqua-remote

**Never lose your Claude Code Remote Control link again.**

aqua-remote monitors your Claude Code sessions running in tmux, auto-recovers broken RC connections, and sends you the link on Telegram, Discord, or email — so you can always connect from your phone or another device.

## The Problem

Claude Code's `/remote-control` gives you a URL to connect from anywhere. But:
- The link expires when the connection drops
- You have to manually type `/remote-control` again to get a new one
- If you're away from your terminal, you're locked out
- If you run multiple sessions, you need to track multiple links

## The Solution

```
aqua-remote start --session sol:0 --name pilot
```

That's it. aqua-remote will:
1. Send `/remote-control` to your session
2. Send you the RC link on Telegram/Discord/email
3. Monitor the connection every 30 seconds
4. Auto-recover if the connection drops (disconnect → reconnect → send new link)
5. Alert you if something goes wrong
6. All alerts include the **session name** so you know which window it's for

## Requirements

- **tmux** — your Claude Code session must run inside tmux
- **Python 3.10+** — no pip dependencies needed (stdlib only)
- **Claude Code** with Remote Control support

### Not using tmux yet?

tmux lets your terminal sessions survive disconnects and run in the background.

```bash
# Install
sudo apt install tmux        # Ubuntu/Debian
brew install tmux             # macOS

# Start a session
tmux new-session -s work

# Run Claude Code inside it
claude

# Detach (session keeps running): Ctrl+B, then D
# Re-attach later:
tmux attach -t work
```

Quick tmux cheatsheet:
| Shortcut | Action |
|----------|--------|
| `Ctrl+B, C` | New window |
| `Ctrl+B, N` | Next window |
| `Ctrl+B, P` | Previous window |
| `Ctrl+B, D` | Detach (keeps running) |
| `tmux ls` | List sessions |
| `tmux attach -t NAME` | Re-attach |

## Setup (2 minutes)

### 1. Clone

```bash
git clone https://github.com/prezis/aqua-remote.git
cd aqua-remote
```

### 2. Configure notifications

```bash
python3 src/setup.py
```

This wizard walks you through setting up Telegram, Discord, or email alerts.

**Telegram setup** (recommended):
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow instructions, get the bot token
3. Add the bot to your group/channel (or start a DM with it)
4. Get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Paste both into the setup wizard

**Discord setup:**
1. Server Settings → Integrations → Webhooks → New Webhook
2. Copy the webhook URL
3. Paste into setup wizard

**Email setup:**
- Works with Gmail (App Passwords), Outlook, or any SMTP server

### 3. Start monitoring

```bash
# Monitor a specific tmux window
python3 src/cli.py start --session sol:0 --name pilot

# Monitor multiple sessions
python3 src/cli.py start --session sol:0 --name pilot
python3 src/cli.py start --session work:2 --name backend
```

### 4. Install watchdog (optional but recommended)

```bash
python3 src/cli.py install
```

This adds a cron job that checks every 5 minutes if your monitors are alive, and restarts them if they crash (OOM, etc).

## Usage

```bash
# Check status of all monitors
python3 src/cli.py status

# Stop a monitor
python3 src/cli.py stop --name pilot

# Test notifications
python3 src/cli.py test

# Run in foreground (for debugging)
python3 src/cli.py start --session sol:0 --name pilot --foreground
```

## How It Works

```text
+----------------+     +---------------+     +----------------+
|  Claude Code   |---->|    monitor    |---->|  Telegram /    |
|  in tmux       |     |   (Python)    |     |  Discord /     |
|  sol:0         |<----|               |     |  Email         |
+----------------+     +-------+-------+     +----------------+
                                |
                        +-------v-------+
                        |   watchdog    |
                        |   (cron)      |
                        |   restarts    |
                        |   if dead     |
                        +---------------+
```

1. **Monitor** captures tmux output every 30s, detects RC URLs and connection state
2. **Auto-recovery** triggers after 15 min idle: clears stale state, sends `/remote-control`, auto-accepts the Continue menu
3. **Heartbeat file** written every 30s — watchdog cron checks it every 5 min
4. **If monitor dies** (OOM, crash) → watchdog restarts it and alerts you

> **Note:** Claude Code has no `/disconnect` command. Recovery uses `Ctrl+C` + `Escape` to clear stale "reconnecting" state, then sends a fresh `/remote-control`. The RC menu (Continue/Disconnect) is auto-handled.

## Config

Config lives in `~/.aqua-remote/config.json`:

```json
{
  "channel": "telegram",
  "telegram_bot_token": "123456789:ABC...",
  "telegram_chat_id": "-1001234567890"
}
```

Logs: `~/.aqua-remote/logs/`
State: `~/.aqua-remote/state/`
Heartbeats: `~/.aqua-remote/heartbeats/`

## FAQ

**Q: Can I monitor multiple sessions?**
A: Yes! Run `start` for each session with a different `--name`. Each gets its own monitor, heartbeat, and alerts (with session name included).

**Q: What if my machine reboots?**
A: The cron watchdog will restart monitors, but you need to restart your tmux sessions and Claude Code first. Consider adding startup scripts.

**Q: Does it work without tmux?**
A: No. tmux is required because aqua-remote reads session output via `tmux capture-pane`. If you're not using tmux yet, see the setup section above.

**Q: Is this safe? Does it send my code/conversations?**
A: aqua-remote only reads the last 80 lines of tmux output to detect RC URLs and connection state. It never reads, stores, or sends your code or conversation content. The only data sent to your notification channel is: RC links, connection status alerts, and session names.

**Q: Where are my credentials stored?**
A: In `~/.aqua-remote/config.json` with chmod 600 (owner-only read). See the Security section below.

## Security

- **Config file** (`~/.aqua-remote/config.json`) stores notification credentials (bot tokens, webhook URLs, SMTP passwords) in **plaintext** with `chmod 600` (owner-read-only).
- **Do not commit** `config.json` to version control. It is listed in `.gitignore`.
- The monitor only reads tmux pane output to detect RC URLs and connection state. It does not access your code, conversation content, or any files outside `~/.aqua-remote/`.
- PID files and heartbeats in `~/.aqua-remote/` contain only process IDs and timestamps.

## Contributing

PRs welcome! Please open an issue first for large changes.

## License

MIT
