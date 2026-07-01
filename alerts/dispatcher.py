"""
Alert dispatcher.

Decides whether a signal becomes an alert, and if so, formats it and pushes it
to every registered channel. Implements the Module 6 requirements:

  * Priority      — derived from the signal's confidence/risk.
  * Duplicate prevention + cooldown — a per (index, direction, kind) timer so
    the same setup does not spam the channel.
  * Min-priority gate — low-priority signals are recorded but not pushed.

Persists every decision (sent / suppressed / failed) to the alerts table when a
database is supplied, giving an auditable history.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence

from alerts.channels import NotificationChannel
from alerts.formatting import format_signal_html
from alerts.models import Alert, Priority, PriorityConfig, map_priority
from signals.models import Signal

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Turns signals into channel notifications under a priority/cooldown policy."""

    def __init__(
        self,
        channels: Sequence[NotificationChannel],
        min_priority: Priority = Priority.MEDIUM,
        cooldown_sec: int = 180,
        priority_config: PriorityConfig | None = None,
        database: object | None = None,   # Database | None (loose to avoid import cycle)
    ) -> None:
        self._channels = list(channels)
        self._min_priority = min_priority
        self._cooldown_sec = cooldown_sec
        self._priority_config = priority_config or PriorityConfig()
        self._db = database
        self._last_sent: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    def dispatch(self, signal: Signal) -> Alert | None:
        """Evaluate and (if warranted) push an alert for ``signal``."""
        priority = map_priority(signal, self._priority_config)
        message = format_signal_html(signal, priority)
        key = (signal.index_name, signal.direction.value, signal.kind.value)

        # Priority gate.
        if priority < self._min_priority:
            return self._record(signal, priority, message, "console", "suppressed", push=False)

        # Cooldown / duplicate prevention.
        if self._in_cooldown(key):
            logger.debug("Alert for %s in cooldown; suppressing.", key)
            return self._record(signal, priority, message, "console", "suppressed", push=False)

        # Fan out to channels.
        any_sent = False
        any_failed = False
        for channel in self._channels:
            ok = channel.send(message)
            any_sent = any_sent or ok
            any_failed = any_failed or (not ok)
        if any_sent:
            self._mark_sent(key)

        status = "sent" if any_sent else "failed"
        channel_names = ",".join(c.name for c in self._channels) or "none"
        return self._record(signal, priority, message, channel_names, status, push=False)

    # ---- cooldown -----------------------------------------------------------
    def _in_cooldown(self, key: tuple[str, str, str]) -> bool:
        with self._lock:
            last = self._last_sent.get(key)
            return last is not None and (time.time() - last) < self._cooldown_sec

    def _mark_sent(self, key: tuple[str, str, str]) -> None:
        with self._lock:
            self._last_sent[key] = time.time()

    # ---- persistence --------------------------------------------------------
    def _record(
        self, signal: Signal, priority: Priority, message: str,
        channel: str, status: str, push: bool,
    ) -> Alert:
        alert = Alert(
            index_name=signal.index_name,
            priority=priority,
            direction=signal.direction.value,
            kind=signal.kind.value,
            confidence=signal.confidence,
            message=message,
            channel=channel,
            status=status,
        )
        if self._db is not None:
            try:
                self._db.insert_alert(alert)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - never let logging break dispatch
                logger.exception("Failed to persist alert.")
        return alert
