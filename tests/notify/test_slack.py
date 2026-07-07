"""Tests for the Slack channel (simple JSON POST with a ``text`` field)."""

from __future__ import annotations

import json

import pytest

from backuphelper.config.models import SimpleUrlChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.slack import SlackChannel


def _recorder():
    sent: dict = {}

    def transport(url, data, headers):
        sent["url"] = url
        sent["data"] = data
        sent["headers"] = dict(headers)

    return sent, transport


def _event():
    return AlertEvent(
        status="warning",
        title="Backup degraded",
        message="1 source skipped",
        instance="prod",
        snapshot_id="snap-9",
    )


def test_slack_posts_text_payload_to_url():
    cfg = SimpleUrlChannelConfig(url="https://hooks.slack.com/services/XXX")
    sent, transport = _recorder()
    SlackChannel(cfg, transport=transport).send(_event())

    assert sent["url"] == "https://hooks.slack.com/services/XXX"
    assert sent["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(sent["data"])
    assert set(body.keys()) == {"text"}
    assert "Backup degraded" in body["text"]
    assert "1 source skipped" in body["text"]
    assert "prod" in body["text"]


def test_slack_without_url_raises():
    with pytest.raises(ValueError):
        SlackChannel(SimpleUrlChannelConfig(), transport=lambda *a, **k: None).send(_event())
