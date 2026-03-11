# aqua-remote

**Never lose your Claude Code Remote Control link again.**

aqua-remote monitors your Claude Code sessions running in tmux, auto-recovers broken RC connections, and sends you the new link via Telegram, Discord, or email — so you can always connect from your phone.

---

## The Problem

Claude Code's `/remote-control` gives you a URL to connect from anywhere. But:
- The link expires when the connection drops
- You have to manually type `/remote-control` again to get a new one
- If you're away from your terminal, you're locked out
- RC sometimes gets stuck in "reconnecting" state

## The Solution

```
python3 src/cli.py start --session sol:0 --name pilot
```

aqua-remote will:
1. **Detect** when RC drops or gets stuck
2. **Auto-recover** the connection (clear stale state, send fresh `/remote-control`)
3. **Send you the new RC link** on Telegram / Discord / email — one message, clickable
4. **Protect your input** — if you're typing on your phone via RC, recovery waits
5. **Restart itself** if it crashes (via optional cron watchdog)

---

## Quick Start

### 1. Requirements

- **Python 3.10+** — no pip dependencies (stdlib only)
- **tmux** — your Claude Code session must run inside tmux
- **Claude Code** with Remote Control support

### 2. Clone

```bash
git clone https://github.com/prezis/aqua-remote.git
cd aqua-remote
```

### 3. Configure notifications

```bash
python3 src/setup.py
```

The setup wizard walks you through choosing and configuring your notification channel.

### 4. Start monitoring

```bash
python3 src/cli.py start --session sol:0 --name pilot
```

### 5. Install watchdog (optional)

```bash
python3 src/cli.py install
```

Adds a cron job that checks every 5 minutes if your monitor is alive and restarts it if needed.

---

## Notification Channels

### Telegram (recommended)

Best for mobile — instant push notifications with a clickable RC link.

**Step 1: Create a bot**

1. Open Telegram, search for **@BotFather** (or go to [t.me/BotFather](https://t.me/BotFather))
2. Send `/newbot`
3. Choose a name (e.g. "RC Monitor") and a username (must end in `bot`)
4. Copy the **bot token** — looks like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`

**Step 2: Create a group and add the bot**

1. Create a new Telegram group (e.g. "RC Alerts")
2. Add your bot to the group
3. **Send at least one message** in the group — the bot needs this to detect the chat

Or for direct messages: open a chat with your bot and press "Start".

**Step 3: Get the chat ID**

Open this URL in your browser (replace `YOUR_BOT_TOKEN`):

```
https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
```

Find `"chat":{"id":` in the JSON:
- **Group IDs** start with `-` (e.g. `-1001234567890`)
- **DM IDs** are positive numbers (e.g. `123456789`)

> **Empty response?** Make sure you sent a message *after* adding the bot. Remove and re-add the bot if needed.

**Step 4: Run setup**

```bash
python3 src/setup.py
# Choose "1. Telegram"
# Paste bot token
# Paste chat ID
# Check your Telegram for the test message
```

### Discord

1. **Server Settings → Integrations → Webhooks → New Webhook**
2. Name it, choose the channel, click **Copy Webhook URL**
3. Paste into the setup wizard

### Email

Works with Gmail (App Passwords), Outlook, or any SMTP server.

For Gmail: enable 2FA → create an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

---

## Usage

```bash
# Start monitoring
python3 src/cli.py start --session sol:0 --name pilot

# Check status
python3 src/cli.py status

# Stop monitoring
python3 src/cli.py stop --name pilot

# Test notifications
python3 src/cli.py test

# Run in foreground (for debugging)
python3 src/cli.py start --session sol:0 --name pilot --foreground

# Force restart if already running
python3 src/cli.py start --session sol:0 --name pilot --force

# Monitor multiple sessions
python3 src/cli.py start --session sol:0 --name pilot
python3 src/cli.py start --session work:2 --name backend
```

---

## How It Works

```
┌────────────────┐     ┌───────────────┐     ┌────────────────┐
│  Claude Code   │────>│    monitor    │────>│  Telegram /    │
│  in tmux       │     │   (Python)    │     │  Discord /     │
│  sol:0         │<────│               │     │  Email         │
└────────────────┘     └───────┬───────┘     └────────────────┘
                               │
                       ┌───────▼───────┐
                       │   watchdog    │
                       │   (cron)      │
                       │   restarts    │
                       │   if dead     │
                       └───────────────┘
```

1. **Monitor** captures tmux output every 15s, detects RC state and URLs
2. **Auto-recovery** triggers when RC drops (connected → gone) or enters "reconnecting" state
3. **User protection** — if you used RC in the last 60s, recovery waits so it doesn't corrupt your input
4. **Recovery process**: clears stale state → sends `/remote-control` → auto-accepts Continue menu → sends you the new link
5. **Single notification** — one Telegram message with clickable link (no spam)
6. **Heartbeat file** every 15s — watchdog cron restarts the monitor if heartbeat goes stale

### What triggers recovery

| Trigger | Description |
|---------|-------------|
| RC drop | RC was "connected" but status disappeared |
| Reconnecting | RC stuck in "reconnecting" state (>10 min) |
| Idle + no RC | Session idle >2 min and RC not connected |

### What does NOT trigger recovery

| Situation | Why |
|-----------|-----|
| Idle + RC connected | Normal — Claude waiting for your input |
| Pilot busy | RC works even during processing — command queues |
| User recently active | Waits 60s after last user interaction |

### Recovery during busy sessions

`/remote-control` sent while Claude is busy **queues in the terminal buffer** and executes when the current task finishes. The monitor detects this (sees the command on the prompt line) and waits patiently instead of re-sending.

---

## Not Using tmux Yet?

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

| Shortcut | Action |
|----------|--------|
| `Ctrl+B, C` | New window |
| `Ctrl+B, N` | Next window |
| `Ctrl+B, D` | Detach (keeps running) |
| `tmux ls` | List sessions |
| `tmux attach -t NAME` | Re-attach |

---

## Config & Files

Config: `~/.aqua-remote/config.json` (chmod 600, owner-only)

```json
{
  "channel": "telegram",
  "telegram_bot_token": "123456789:ABC...",
  "telegram_chat_id": "-1001234567890"
}
```

Runtime files:

| Path | Purpose |
|------|---------|
| `~/.aqua-remote/logs/` | Monitor logs |
| `~/.aqua-remote/state/` | Recovery state (last URL, counters) |
| `~/.aqua-remote/heartbeats/` | Heartbeat timestamps |
| `~/.aqua-remote/pids/` | PID files |

---

## FAQ

**Can I monitor multiple sessions?**
Yes. Run `start` with a different `--name` for each. Each gets its own monitor, heartbeat, and alerts.

**What if my machine reboots?**
The cron watchdog restarts monitors, but you need to restart tmux and Claude Code first.

**Does it work without tmux?**
No. tmux is required — aqua-remote reads output via `tmux capture-pane`.

**Is this safe? Does it read my code?**
aqua-remote only reads the last 80 lines of tmux output to detect RC URLs and connection state. It never reads, stores, or sends your code or conversations. Only RC links and status alerts go to your notification channel.

**Where are my credentials stored?**
In `~/.aqua-remote/config.json` with chmod 600 (owner-only read). Never commit this file.

**What if I'm typing on my phone and recovery triggers?**
It won't. aqua-remote tracks when you last interacted via RC. If you were active in the last 60 seconds, recovery waits.

**What about the recovery spam I used to get?**
Fixed. Recovery now sends a single message with the clickable RC link. No more "recovering... reconnected... new link" spam.

---

## Project Structure

```
aqua-remote/
├── src/
│   ├── cli.py        # CLI commands (start/stop/status/test/install/setup)
│   ├── monitor.py    # Core monitoring loop + recovery logic
│   ├── notify.py     # Notification channels (Telegram/Discord/Email)
│   ├── setup.py      # Interactive setup wizard
│   └── watchdog.sh   # Cron watchdog script
├── pyproject.toml
├── LICENSE            # MIT
└── README.md
```

Zero external dependencies — stdlib only.

---

## License

MIT
