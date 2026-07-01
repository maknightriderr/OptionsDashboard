"""
Offline tests for Phase 4: priority mapping, HTML formatting, the dispatcher's
priority gate / cooldown / failure handling, and alert persistence. No network:
channels are fakes, so nothing is actually sent to Telegram.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alerts.channels import NotificationChannel
from alerts.dispatcher import AlertDispatcher
from alerts.formatting import format_signal_html
from alerts.models import Alert, Priority, PriorityConfig, map_priority
from database.sqlite_db import SQLiteDatabase
from signals.models import Direction, Signal, SignalKind


def _signal(confidence: int = 75, risk: int = 30, direction: Direction = Direction.BULLISH) -> Signal:
    return Signal(
        index_name="NIFTY", direction=direction, kind=SignalKind.REVERSAL, spot=24050,
        confidence=confidence, risk=risk, probability=70,
        entry=24050, stop_loss=24000, target1=24100, target2=24150, target3=24200,
        reason="PCR elevated; near support", supporting_indicators=["PCR", "Support"],
    )


class FakeChannel(NotificationChannel):
    def __init__(self, name: str = "fake", succeed: bool = True) -> None:
        self.name = name
        self._succeed = succeed
        self.sent: list[str] = []

    def send(self, message: str) -> bool:
        if self._succeed:
            self.sent.append(message)
        return self._succeed


# --------------------------------------------------------------------------- #
# priority
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("conf,risk,expected", [
    (90, 20, Priority.CRITICAL),
    (90, 60, Priority.HIGH),    # high conf but risk too high for critical
    (72, 30, Priority.HIGH),
    (60, 30, Priority.MEDIUM),
    (40, 30, Priority.LOW),
])
def test_map_priority(conf, risk, expected) -> None:
    assert map_priority(_signal(conf, risk)) is expected


def test_priority_ordering() -> None:
    assert Priority.CRITICAL > Priority.HIGH > Priority.MEDIUM > Priority.LOW
    assert Priority.from_name("high") is Priority.HIGH


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def test_format_signal_html_contains_key_fields() -> None:
    msg = format_signal_html(_signal(), Priority.HIGH)
    assert "NIFTY" in msg and "BULLISH" in msg
    assert "Entry:" in msg and "Stop:" in msg and "Targets:" in msg
    assert "not trading advice" in msg.lower()


def test_format_escapes_html() -> None:
    sig = _signal()
    sig.reason = "danger <script> & co"
    msg = format_signal_html(sig, Priority.LOW)
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #
def test_dispatch_sends_high_priority() -> None:
    ch = FakeChannel()
    d = AlertDispatcher([ch], min_priority=Priority.MEDIUM, cooldown_sec=300)
    alert = d.dispatch(_signal(confidence=80, risk=30))
    assert alert is not None and alert.status == "sent"
    assert len(ch.sent) == 1


def test_dispatch_gates_low_priority() -> None:
    ch = FakeChannel()
    d = AlertDispatcher([ch], min_priority=Priority.HIGH, cooldown_sec=300)
    alert = d.dispatch(_signal(confidence=60, risk=30))  # MEDIUM < HIGH
    assert alert is not None and alert.status == "suppressed"
    assert ch.sent == []


def test_dispatch_cooldown_suppresses_duplicate() -> None:
    ch = FakeChannel()
    d = AlertDispatcher([ch], min_priority=Priority.MEDIUM, cooldown_sec=10_000)
    first = d.dispatch(_signal(confidence=80))
    second = d.dispatch(_signal(confidence=80))
    assert first.status == "sent"
    assert second.status == "suppressed"
    assert len(ch.sent) == 1


def test_dispatch_reports_channel_failure() -> None:
    ch = FakeChannel(succeed=False)
    d = AlertDispatcher([ch], min_priority=Priority.LOW, cooldown_sec=0)
    alert = d.dispatch(_signal(confidence=80))
    assert alert.status == "failed"


def test_dispatch_persists_to_db(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema()
    d = AlertDispatcher([FakeChannel()], min_priority=Priority.MEDIUM,
                        cooldown_sec=300, database=db)
    d.dispatch(_signal(confidence=80))
    rows = db.fetch_recent_alerts("NIFTY", limit=5)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["priority"] == int(Priority.HIGH)
    db.close()


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_alert_roundtrip(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema()
    alert = Alert(index_name="NIFTY", priority=Priority.CRITICAL, direction="bullish",
                  kind="reversal", confidence=90, message="<b>hi</b>",
                  channel="telegram", status="sent")
    new_id = db.insert_alert(alert)
    assert new_id > 0
    rows = db.fetch_recent_alerts("NIFTY")
    assert rows[0]["confidence"] == 90 and rows[0]["channel"] == "telegram"
    db.close()
