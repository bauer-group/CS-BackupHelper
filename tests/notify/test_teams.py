"""Tests for the Microsoft Teams channel (Adaptive Card + legacy MessageCard)."""

from __future__ import annotations

import json

import pytest

from backuphelper.config.models import TeamsChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.teams import ADAPTIVE_COLOR, THEME_COLOR, TeamsChannel

URL = "https://outlook.office.com/webhook/XXX"


def _recorder():
    sent: dict = {}

    def transport(url, data, headers):
        sent["url"] = url
        sent["data"] = data
        sent["headers"] = dict(headers)

    return sent, transport


def _event(status="error"):
    return AlertEvent(
        status=status,
        title="Backup failed",
        message="db dump errored",
        instance="prod",
        snapshot_id="snap-1",
    )


def test_adaptive_builds_teams_envelope_with_v14_card():
    cfg = TeamsChannelConfig(url=URL, format="adaptive")
    sent, transport = _recorder()
    TeamsChannel(cfg, transport=transport).send(_event())

    assert sent["url"] == URL
    assert sent["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(sent["data"])
    assert body["type"] == "message"
    attachment = body["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.4"

    texts = [b for b in card["body"] if b.get("type") == "TextBlock"]
    joined = " ".join(t.get("text", "") for t in texts)
    assert "Backup failed" in joined
    assert "db dump errored" in joined
    # status color surfaces on a TextBlock
    assert any(t.get("color") == ADAPTIVE_COLOR["error"] for t in texts)


def test_messagecard_builds_legacy_card_with_theme_color():
    cfg = TeamsChannelConfig(url=URL, format="messagecard")
    sent, transport = _recorder()
    TeamsChannel(cfg, transport=transport).send(_event())

    body = json.loads(sent["data"])
    assert body["@type"] == "MessageCard"
    assert body["@context"] == "http://schema.org/extensions"
    assert body["themeColor"] == THEME_COLOR["error"]
    assert body["title"] == "Backup failed"
    assert "db dump errored" in body["text"]


@pytest.mark.parametrize("status", ["success", "warning", "error"])
def test_messagecard_theme_color_tracks_status(status):
    cfg = TeamsChannelConfig(url=URL, format="messagecard")
    sent, transport = _recorder()
    TeamsChannel(cfg, transport=transport).send(_event(status))
    assert json.loads(sent["data"])["themeColor"] == THEME_COLOR[status]


def test_theme_colors_are_distinct_green_amber_red():
    assert len({THEME_COLOR["success"], THEME_COLOR["warning"], THEME_COLOR["error"]}) == 3


def test_teams_without_url_raises():
    with pytest.raises(ValueError):
        TeamsChannel(
            TeamsChannelConfig(format="adaptive"), transport=lambda *a, **k: None
        ).send(_event())
