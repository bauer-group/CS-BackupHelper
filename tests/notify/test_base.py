"""Tests for the shared notification contract (AlertEvent + Channel ABC)."""

from __future__ import annotations

import pytest

from backuphelper.notify.base import AlertEvent, Channel


def test_alert_event_minimal_and_defaults():
    ev = AlertEvent(status="error", title="Backup failed", message="disk full")
    assert ev.status == "error"
    assert ev.title == "Backup failed"
    assert ev.message == "disk full"
    # Defaults
    assert ev.instance == ""
    assert ev.snapshot_id == ""
    assert ev.job == ""
    assert ev.duration_seconds == 0.0
    assert ev.total_bytes == 0
    assert ev.errors == []
    assert ev.metrics == {}


def test_alert_event_mutable_defaults_are_not_shared():
    a = AlertEvent(status="success", title="a", message="b")
    b = AlertEvent(status="success", title="c", message="d")
    a.errors.append("boom")
    a.metrics["workflows_count"] = 3
    assert b.errors == []
    assert b.metrics == {}


def test_channel_is_abstract_and_requires_send():
    with pytest.raises(TypeError):
        Channel()  # type: ignore[abstract]


def test_channel_subclass_declares_name_and_send():
    class Dummy(Channel):
        name = "dummy"

        def __init__(self, cfg):
            self.cfg = cfg

        def send(self, event: AlertEvent) -> None:
            self.last = event

    d = Dummy({"x": 1})
    assert d.name == "dummy"
    ev = AlertEvent(status="warning", title="t", message="m")
    d.send(ev)
    assert d.last is ev
