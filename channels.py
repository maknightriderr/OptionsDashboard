"""
Notification channels.

A channel is anything that can deliver a formatted message. The abstraction
keeps the dispatcher decoupled from Telegram specifically, so desktop
notifications, sound, or a webhook can be added later as new channels without
touching dispatch logic (open/closed principle).

TelegramChannel posts directly to the Bot API over HTTPS via requests — no
heavyweight SDK needed just to send a message. The interactive command bot
(alerts/telegram_bot.py) is the only place that needs python-telegram-bot.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger(__name__)


class NotificationChannel(ABC):
    """Delivers a single formatted message; returns True on success."""

    name: str = "channel"

    @abstractmethod
    def send(self, message: str) -> bool:
        ...


class ConsoleChannel(NotificationChannel):
    """Prints to logs. Useful for local runs and as a test double."""

    name = "console"

    def send(self, message: str) -> bool:
        logger.info("ALERT\n%s", message)
        return True


class TelegramChannel(NotificationChannel):
    """Sends HTML messages to a chat via the Telegram Bot API."""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, message: str) -> bool:
        try:
            response = requests.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
            if response.status_code == 200 and response.json().get("ok"):
                return True
            logger.error("Telegram send failed: %s %s", response.status_code, response.text[:200])
            return False
        except requests.RequestException as exc:
            logger.error("Telegram send error: %s", exc)
            return False
