"""
Dispatcher factory.

Builds an AlertDispatcher from settings: always includes a console channel (so
something is logged locally), and adds Telegram when a bot token + chat id are
configured. Returns None only if explicitly disabled — by default you still get
console alerts even without Telegram set up.
"""

from __future__ import annotations

import logging

from alerts.channels import ConsoleChannel, NotificationChannel, TelegramChannel
from alerts.dispatcher import AlertDispatcher
from alerts.models import Priority
from config.settings import Settings

logger = logging.getLogger(__name__)


def build_dispatcher(settings: Settings, database: object | None = None) -> AlertDispatcher:
    """Construct an AlertDispatcher with the channels available from settings."""
    channels: list[NotificationChannel] = [ConsoleChannel()]

    token, chat_id = settings.resolve_telegram()
    if token and chat_id:
        channels.append(TelegramChannel(token, chat_id))
        logger.info("Telegram alert channel enabled.")
    else:
        logger.info("Telegram not configured; alerts go to console only.")

    return AlertDispatcher(
        channels=channels,
        min_priority=Priority.from_name(settings.alert_min_priority),
        cooldown_sec=settings.alert_cooldown_sec,
        database=database,
    )
