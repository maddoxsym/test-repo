"""Optional notification hooks.

Fully optional by design: the bot operates identically with notifications
disabled, and a failing channel is logged but never propagates. The interface
is deliberately minimal so Telegram, Discord, or email can be added without
touching the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..config.loader import optional_env
from ..config.schema import NotificationsConfig
from ..utils.logging import get_logger

log = get_logger(__name__)

#: Events considered important enough to notify about.
ALERT_EVENTS = (
    "bot_started",
    "bot_stopped",
    "demo_connection_lost",
    "demo_connection_restored",
    "safety_lock",
    "order_error",
    "day_14_complete",
    "champion_selected",
    "champion_changed",
)


class NotificationChannel(ABC):
    """One delivery mechanism."""

    name: str = "channel"

    @abstractmethod
    async def send(self, title: str, message: str) -> bool:
        """Deliver a message. Returns success; must never raise."""

    def is_configured(self) -> bool:
        return True


class ConsoleChannel(NotificationChannel):
    """Always available; writes the alert to the log."""

    name = "console"

    async def send(self, title: str, message: str) -> bool:
        log.info("START", f"NOTIFY [{title}] {message}")
        return True


class TelegramChannel(NotificationChannel):
    """Telegram bot API. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""

    name = "telegram"

    def __init__(self) -> None:
        self._token = optional_env("TELEGRAM_BOT_TOKEN")
        self._chat_id = optional_env("TELEGRAM_CHAT_ID")

    def is_configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(self, title: str, message: str) -> bool:
        if not self.is_configured():
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.post(
                    url, json={"chat_id": self._chat_id, "text": f"*{title}*\n{message}",
                               "parse_mode": "Markdown"}
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            log.debug("START", f"Telegram notification failed: {exc}")
            return False
        return True


class DiscordChannel(NotificationChannel):
    """Discord webhook. Needs DISCORD_WEBHOOK_URL."""

    name = "discord"

    def __init__(self) -> None:
        self._webhook = optional_env("DISCORD_WEBHOOK_URL")

    def is_configured(self) -> bool:
        return bool(self._webhook)

    async def send(self, title: str, message: str) -> bool:
        if not self._webhook:
            return False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.post(
                    self._webhook, json={"content": f"**{title}**\n{message}"}
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            log.debug("START", f"Discord notification failed: {exc}")
            return False
        return True


class NotificationManager:
    """Fans an alert out to every configured channel."""

    def __init__(self, config: NotificationsConfig) -> None:
        self.config = config
        self.channels: list[NotificationChannel] = []
        if not config.enabled:
            return

        if config.console:
            self.channels.append(ConsoleChannel())
        for enabled, channel_cls in (
            (config.telegram, TelegramChannel),
            (config.discord, DiscordChannel),
        ):
            if not enabled:
                continue
            channel = channel_cls()
            if channel.is_configured():
                self.channels.append(channel)
            else:
                log.info(
                    "START",
                    f"Notification channel '{channel.name}' enabled but not configured — skipped",
                )

        if self.channels:
            log.info(
                "START",
                f"Notifications active: {', '.join(c.name for c in self.channels)}",
            )

    async def notify(self, title: str, message: str) -> dict[str, bool]:
        """Send to every channel; failures are recorded, never raised."""
        results: dict[str, bool] = {}
        for channel in self.channels:
            try:
                results[channel.name] = await channel.send(title, message)
            except Exception as exc:  # noqa: BLE001 - notifications must never break the bot
                log.debug("START", f"Notification channel {channel.name} raised: {exc}")
                results[channel.name] = False
        return results

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "channels": [c.name for c in self.channels],
        }
