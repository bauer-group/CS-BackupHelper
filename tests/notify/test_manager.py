"""Tests for the AlertManager: severity gating + fault-isolated fan-out."""

from __future__ import annotations

import logging

import pytest

from backuphelper.config.models import NotifyConfig
from backuphelper.notify import manager as manager_mod
from backuphelper.notify.base import AlertEvent, Channel
from backuphelper.notify.discord import DiscordChannel
from backuphelper.notify.email import EmailChannel
from backuphelper.notify.healthchecks import HealthchecksChannel
from backuphelper.notify.manager import CHANNELS, AlertManager
from backuphelper.notify.ntfy import NtfyChannel
from backuphelper.notify.slack import SlackChannel
from backuphelper.notify.teams import TeamsChannel
from backuphelper.notify.webhook import WebhookChannel


def _recording_channel(name: str, bucket: list):
    class Rec(Channel):
        def __init__(self, cfg):
            self.cfg = cfg

        def send(self, event: AlertEvent) -> None:
            bucket.append((name, event))

    Rec.name = name  # type: ignore[misc]
    return Rec


def _raising_channel(name: str, exc: Exception):
    class Boom(Channel):
        def __init__(self, cfg):
            self.cfg = cfg

        def send(self, event: AlertEvent) -> None:
            raise exc

    Boom.name = name  # type: ignore[misc]
    return Boom


def _event(status: str) -> AlertEvent:
    return AlertEvent(status=status, title="t", message="m")


# ---------------------------------------------------------------- registry ---


def test_registry_maps_all_channel_names_to_classes():
    assert CHANNELS == {
        "email": EmailChannel,
        "webhook": WebhookChannel,
        "teams": TeamsChannel,
        "slack": SlackChannel,
        "discord": DiscordChannel,
        "ntfy": NtfyChannel,
        "healthchecks": HealthchecksChannel,
    }


# ------------------------------------------------------------ severity gate ---

GATING = [
    ("errors", "success", False),
    ("errors", "warning", False),
    ("errors", "error", True),
    ("warnings", "success", False),
    ("warnings", "warning", True),
    ("warnings", "error", True),
    ("all", "success", True),
    ("all", "warning", True),
    ("all", "error", True),
]


@pytest.mark.parametrize("level,status,should_send", GATING)
def test_severity_gating_matrix(monkeypatch, level, status, should_send):
    bucket: list = []
    monkeypatch.setattr(
        manager_mod, "CHANNELS", {"webhook": _recording_channel("webhook", bucket)}
    )
    cfg = NotifyConfig(channels=["webhook"], level=level)
    AlertManager(cfg).notify(_event(status))
    assert bool(bucket) is should_send


# ------------------------------------------------------------- empty config ---


def test_empty_channels_is_a_noop(monkeypatch):
    bucket: list = []
    # Even a channel that would explode on construction must never be built.
    def _explode(cfg):
        raise AssertionError("must not construct any channel")

    monkeypatch.setattr(manager_mod, "CHANNELS", {"webhook": _explode})
    cfg = NotifyConfig(channels=[], level="all")
    AlertManager(cfg).notify(_event("error"))  # no raise
    assert bucket == []


# --------------------------------------------------------- channel selection ---


def test_only_named_channels_are_built(monkeypatch):
    bucket: list = []
    monkeypatch.setattr(
        manager_mod,
        "CHANNELS",
        {
            "slack": _recording_channel("slack", bucket),
            "webhook": _recording_channel("webhook", bucket),
        },
    )
    cfg = NotifyConfig(channels=["slack"], level="all")
    AlertManager(cfg).notify(_event("error"))
    assert [name for name, _ in bucket] == ["slack"]


def test_unknown_channel_name_is_skipped(monkeypatch, caplog):
    bucket: list = []
    monkeypatch.setattr(
        manager_mod, "CHANNELS", {"slack": _recording_channel("slack", bucket)}
    )
    cfg = NotifyConfig(channels=["nope", "slack"], level="all")
    with caplog.at_level(logging.WARNING):
        AlertManager(cfg).notify(_event("error"))
    assert [name for name, _ in bucket] == ["slack"]


# ----------------------------------------------------------- fault isolation ---


def test_one_failing_channel_does_not_block_the_others(monkeypatch):
    bucket: list = []
    monkeypatch.setattr(
        manager_mod,
        "CHANNELS",
        {
            "webhook": _raising_channel("webhook", RuntimeError("network down")),
            "slack": _recording_channel("slack", bucket),
        },
    )
    cfg = NotifyConfig(channels=["webhook", "slack"], level="all")
    # Must not raise, and the healthy channel must still receive the event.
    AlertManager(cfg).notify(_event("error"))
    assert [name for name, _ in bucket] == ["slack"]


def test_channel_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(
        manager_mod,
        "CHANNELS",
        {"webhook": _raising_channel("webhook", RuntimeError("boom"))},
    )
    cfg = NotifyConfig(channels=["webhook"], level="all")
    with caplog.at_level(logging.ERROR):
        AlertManager(cfg).notify(_event("error"))
    assert any("webhook" in r.getMessage() for r in caplog.records)


def test_construction_failure_is_isolated(monkeypatch):
    bucket: list = []

    _bad = type(
        "Bad",
        (Channel,),
        {
            "name": "webhook",
            "__init__": lambda self, cfg: (_ for _ in ()).throw(ValueError("x")),
            "send": lambda self, e: None,
        },
    )
    monkeypatch.setattr(
        manager_mod,
        "CHANNELS",
        {"webhook": _bad, "slack": _recording_channel("slack", bucket)},
    )
    cfg = NotifyConfig(channels=["webhook", "slack"], level="all")
    AlertManager(cfg).notify(_event("error"))
    assert [name for name, _ in bucket] == ["slack"]
