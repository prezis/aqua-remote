# aqua-remote

**Never lose your Claude Code Remote Control link again.**

aqua-remote monitors your Claude Code sessions running in tmux, auto-recovers broken RC connections, and sends you the new link via Telegram, Discord, or email — so you can always connect from your phone.

## The Problem

Claude Code's `/remote-control` gives you a URL to connect from anywhere. But:
- The link expires when the connection drops
- You have to manually type `/remote-control` again to get a new one
- If you're away from your terminal, you're locked out
- RC sometimes shows "connected" but is actually stuck in "connecting" state

## The Solution

```
aqua-remote start --session sol:0 --name pilot
```

That's it. aqua-remote will:
1. Detect when RC drops or gets stuck
2. Auto-recover the connection (disconnect → reconnect)
3. Send you the new RC link on Telegram/Discord/email
4. Protect your input — if you're actively using RC from your phone, recovery waits 60s before touching anything
5. Restart itself if it crashes (via cron watchdog)

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

## Setup

### 1. Clone

```bash
git clone https://github.com/prezis/aqua-remote.git
cd aqua-remote
```

### 2. Configure notifications

```bash
python3 src/setup.py
```

This interactive wizard walks you through setting up your notification channel. See detailed instructions for each channel below.

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

This adds a cron job that checks every 5 minutes if your monitors are alive and restarts them if they crash (OOM, etc).

---

## Notification Channels

### Telegram (recommended)

Telegram is the best option for mobile — you get instant push notifications with your RC link.

#### Step 1: Create a Telegram Bot

1. Open Telegram and search for **@BotFather** (or go to https://t.me/BotFather)
2. Send `/newbot`
3. Choose a name for your bot (e.g. "My Remote Control Bot")
4. Choose a username (must end in `bot`, e.g. `my_rc_monitor_bot`)
5. BotFather will give you a **bot token** — copy it. It looks like: `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`

#### Step 2: Create a Group and Add the Bot

You need a Telegram group (or channel) where the bot will send alerts.

**Option A: Use a group (recommended)**
1. Create a new Telegram group (hamburger menu → New Group)
2. Name it something like "RC Alerts"
3. Add your bot to the group (search for its username)
4. **Important:** Send at least one message in the group (e.g. "hello") — the bot needs this to detect the chat

**Option B: Use a direct message**
1. Open a chat with your bot (search for its username)
2. Press "Start" or send `/start`

#### Step 3: Get the Chat ID

The chat ID tells the bot WHERE to send messages.

1. Open this URL in your browser (replace `YOUR_BOT_TOKEN` with your actual token):
   ```
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```
2. Look for `"chat":{"id":` in the JSON response
   - **Group chat IDs** start with `-` (e.g. `-1001234567890`)
   - **Direct message IDs** are positive numbers (e.g. `123456789`)
3. Copy the full number including the minus sign if present

**Troubleshooting:** If the response is empty (`{"ok":true,"result":[]}`):
- Make sure you sent a message in the group/chat AFTER adding the bot
- Remove the bot from the group, re-add it, and send another message
- Try again — the getUpdates endpoint only shows recent messages

#### Step 4: Enter credentials in setup wizard

```bash
python3 src/setup.py
# Choose "1. Telegram"
# Paste your bot token
# Paste your chat ID
# The wizard sends a test message — check your Telegram!
```

### Discord

1. In your Discord server: **Server Settings → Integrations → Webhooks → New Webhook**
2. Name it (e.g. "aqua-remote"), choose the channel
3. Click **Copy Webhook URL**
4. Paste into the setup wizard

### Email

Works with Gmail (App Passwords), Outlook, or any SMTP server.
- For Gmail: enable 2FA → create an App Password at myaccount.google.com/apppasswords
- The setup wizard will ask for SMTP host, port, username, password, and recipient address

---

## Usage

```bash
# Check status of all monitors
python3 src/cli.py status

# Stop a monitor
python3 src/cli.py stop --name pilot

# Test notifications (sends a test message to your channel)
python3 src/cli.py test

# Run in foreground (for debugging)
python3 src/cli.py start --session sol:0 --name pilot --foreground

# Force restart if already running
python3 src/cli.py start --session sol:0 --name pilot --force
```

## How It Works

```
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
2. **Auto-recovery** triggers after 15 min idle or when "reconnecting" state is detected
3. **User protection** — if you used RC in the last 60s (sent a message from your phone), recovery waits so it doesn't corrupt your input
4. **Recovery process**: clears stale state → sends fresh `/remote-control` → auto-accepts the Continue menu → sends you the new link
5. **Heartbeat file** written every 30s — watchdog cron restarts the monitor if heartbeat goes stale

> **Note:** Claude Code has no `/disconnect` command. Recovery uses `Ctrl+C` + `Escape` to clear stale "reconnecting" state, then sends a fresh `/remote-control`. The RC menu (Continue/Disconnect) is auto-handled.

> **Note:** Recovery does NOT wait for Claude to finish working. The `/remote-control` command queues in the terminal buffer and executes when the current task completes. This way RC stays available even during long-running operations.

## Config

Config lives in `~/.aqua-remote/config.json`:

```json
{
  "channel": "telegram",
  "telegram_bot_token": "123456789:ABC...",
  "telegram_chat_id": "-1001234567890"
}
```

Runtime files:
- Logs: `~/.aqua-remote/logs/`
- State: `~/.aqua-remote/state/`
- Heartbeats: `~/.aqua-remote/heartbeats/`
- PID files: `~/.aqua-remote/pids/`

## FAQ

**Q: Can I monitor multiple sessions?**
A: Yes. Run `start` for each session with a different `--name`. Each gets its own monitor, heartbeat, and alerts.

**Q: What if my machine reboots?**
A: The cron watchdog will restart monitors, but you need to restart your tmux sessions and Claude Code first. Consider adding startup scripts.

**Q: Does it work without tmux?**
A: No. tmux is required because aqua-remote reads session output via `tmux capture-pane`.

**Q: Is this safe? Does it send my code/conversations?**
A: aqua-remote only reads the last 80 lines of tmux output to detect RC URLs and connection state. It never reads, stores, or sends your code or conversation content. The only data sent to your notification channel is: RC links, connection status alerts, and session names.

**Q: Where are my credentials stored?**
A: In `~/.aqua-remote/config.json` with chmod 600 (owner-only read). Do not commit this file — it's in `.gitignore`.

**Q: What if I'm typing on my phone and recovery triggers?**
A: It won't. aqua-remote tracks when you last interacted via RC. If you sent a message in the last 60 seconds, all recovery actions are postponed. After 60s of inactivity, recovery resumes normally.

## Security

- Config file (`~/.aqua-remote/config.json`) stores notification credentials (bot tokens, webhook URLs, SMTP passwords) in **plaintext** with `chmod 600` (owner-only read)
- Do not commit `config.json` to version control — it is in `.gitignore`
- The monitor only reads tmux pane output to detect RC URLs and connection state
- PID files and heartbeats contain only process IDs and timestamps

## License

MIT
