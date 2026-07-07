"""Tests for the Discord channel (simple JSON POST with a ``content`` field)."""

from __future__ import annotations

import json

import pytest

from backuphelper.config.models import SimpleUrlChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.discord import DiscordChannel


def _recorder():
    sent: dict = {}

    def transport(url, data, headers):
        sent["url"] = url
        sent["data"] = data
        sent["headers"] = dict(headers)

    return sent, transport


def _event():
    return AlertEvent(
        status="success",
        title="Backup complete",
        message="all sources captured",
        instance="prod",
    )


def test_discord_posts_content_payload_to_url():
    cfg = SimpleUrlChannelConfig(url="https://discord.com/api/webhooks/XXX/YYY")
    sent, transport = _recorder()
    DiscordChannel(cfg, transport=transport).send(_event())

    assert sent["url"] == "https://discord.com/api/webhooks/XXX/YYY"
    assert sent["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(sent["data"])
    assert "content" in body
    assert "Backup complete" in body["content"]
    assert "all sources captured" in body["content"]


def test_discord_without_url_raises():
    with pytest.raises(ValueError):
        DiscordChannel(SimpleUrlChannelConfig(), transport=lambda *a, **k: None).send(_event())
