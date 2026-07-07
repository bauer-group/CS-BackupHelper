"""Tests for the ntfy channel (plain-text POST, optional bearer auth)."""

from __future__ import annotations

import pytest

from backuphelper.config.models import NtfyChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.ntfy import NtfyChannel


def _recorder():
    sent: dict = {}

    def transport(url, data, headers):
        sent["url"] = url
        sent["data"] = data
        sent["headers"] = dict(headers)

    return sent, transport


def _event():
    return AlertEvent(
        status="error",
        title="Backup failed",
        message="db dump errored",
        instance="prod",
    )


def test_ntfy_posts_message_body_to_url_with_topic():
    cfg = NtfyChannelConfig(url="https://ntfy.sh", topic="backups")
    sent, transport = _recorder()
    NtfyChannel(cfg, transport=transport).send(_event())

    assert sent["url"] == "https://ntfy.sh/backups"
    assert b"db dump errored" in sent["data"]
    assert not any(k.lower() == "authorization" for k in sent["headers"])


def test_ntfy_trailing_slash_url_joins_topic_cleanly():
    cfg = NtfyChannelConfig(url="https://ntfy.sh/", topic="backups")
    sent, transport = _recorder()
    NtfyChannel(cfg, transport=transport).send(_event())
    assert sent["url"] == "https://ntfy.sh/backups"


def test_ntfy_bearer_token_sets_authorization_header():
    cfg = NtfyChannelConfig(url="https://ntfy.sh", topic="backups", token="tok-123")
    sent, transport = _recorder()
    NtfyChannel(cfg, transport=transport).send(_event())
    assert sent["headers"]["Authorization"] == "Bearer tok-123"


def test_ntfy_url_without_topic_posts_to_url():
    cfg = NtfyChannelConfig(url="https://ntfy.sh/backups")
    sent, transport = _recorder()
    NtfyChannel(cfg, transport=transport).send(_event())
    assert sent["url"] == "https://ntfy.sh/backups"


def test_ntfy_without_url_raises():
    with pytest.raises(ValueError):
        NtfyChannel(NtfyChannelConfig(), transport=lambda *a, **k: None).send(_event())
