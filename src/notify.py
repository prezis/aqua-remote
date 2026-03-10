"""
Notification channels for aqua-remote.

Supports: Telegram, Discord webhook, Email (SMTP), stdout (fallback).
Each channel implements send(subject, body) → bool.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.request
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger("aqua-remote.notify")

CONFIG_DIR = Path.home() / ".aqua-remote"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    """Load aqua-remote config from ~/.aqua-remote/config.json."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict) -> None:
    """Save config to ~/.aqua-remote/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# Channel interface
# ---------------------------------------------------------------------------

class NotifyChannel(ABC):
    """Abstract notification channel."""

    @abstractmethod
    def send(self, subject: str, body: str) -> bool:
        """Send a notification. Returns True on success."""
        ...

    @abstractmethod
    def test(self) -> bool:
        """Send a test message. Returns True on success."""
        ...


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TelegramChannel(NotifyChannel):
    """Send notifications via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, subject: str, body: str) -> bool:
        text = f"<b>{subject}</b>\n\n{body}"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("ok", False)
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    def test(self) -> bool:
        return self.send(
            "aqua-remote test",
            "If you see this, Telegram notifications are working.",
        )


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

class DiscordChannel(NotifyChannel):
    """Send notifications via Discord webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, subject: str, body: str) -> bool:
        payload = json.dumps({
            "content": f"**{subject}**\n{body}",
        }).encode()
        req = urllib.request.Request(
            self.webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error("Discord send failed: %s", e)
            return False

    def test(self) -> bool:
        return self.send(
            "aqua-remote test",
            "If you see this, Discord notifications are working.",
        )


# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------

class EmailChannel(NotifyChannel):
    """Send notifications via SMTP email."""

    def __init__(self, smtp_host: str, smtp_port: int,
                 username: str, password: str,
                 from_addr: str, to_addr: str,
                 use_tls: bool = True):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.use_tls = use_tls

    def send(self, subject: str, body: str) -> bool:
        msg = MIMEText(body)
        msg["Subject"] = f"[aqua-remote] {subject}"
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        try:
            if self.use_tls:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
            server.login(self.username, self.password)
            server.sendmail(self.from_addr, [self.to_addr], msg.as_string())
            server.quit()
            return True
        except Exception as e:
            logger.error("Email send failed: %s", e)
            return False

    def test(self) -> bool:
        return self.send(
            "aqua-remote test",
            "If you see this, email notifications are working.",
        )


# ---------------------------------------------------------------------------
# Stdout fallback
# ---------------------------------------------------------------------------

class StdoutChannel(NotifyChannel):
    """Print notifications to stdout (fallback when no channel configured)."""

    def send(self, subject: str, body: str) -> bool:
        print(f"\n{'='*60}")
        print(f"  {subject}")
        print(f"{'='*60}")
        print(body)
        print(f"{'='*60}\n")
        return True

    def test(self) -> bool:
        return self.send("aqua-remote test", "Stdout channel works.")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_channel(cfg: dict | None = None) -> NotifyChannel:
    """Create notification channel from config.

    Config is loaded from ~/.aqua-remote/config.json if not provided.
    Falls back to stdout if no channel is configured.
    """
    if cfg is None:
        cfg = load_config()

    channel_type = cfg.get("channel", "stdout")

    if channel_type == "telegram":
        return TelegramChannel(
            bot_token=cfg["telegram_bot_token"],
            chat_id=cfg["telegram_chat_id"],
        )
    elif channel_type == "discord":
        return DiscordChannel(
            webhook_url=cfg["discord_webhook_url"],
        )
    elif channel_type == "email":
        return EmailChannel(
            smtp_host=cfg.get("smtp_host", "smtp.gmail.com"),
            smtp_port=cfg.get("smtp_port", 587),
            username=cfg["smtp_username"],
            password=cfg["smtp_password"],
            from_addr=cfg["email_from"],
            to_addr=cfg["email_to"],
            use_tls=cfg.get("smtp_tls", True),
        )
    else:
        return StdoutChannel()
