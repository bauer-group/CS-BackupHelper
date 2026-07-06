"""Tests for the webhook channel (JSON POST + HMAC-SHA256 signing)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from backuphelper.config.models import WebhookChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.webhook import WebhookChannel


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
        message="disk full",
        instance="prod",
        job="db",
        snapshot_id="snap-1",
        errors=["e1", "e2"],
        metrics={"records_count": 5, "workflows_count": 2},
    )


def test_webhook_posts_json_payload_to_url():
    cfg = WebhookChannelConfig(url="https://hook.example/endpoint")
    sent, transport = _recorder()
    WebhookChannel(cfg, transport=transport).send(_event())

    assert sent["url"] == "https://hook.example/endpoint"
    assert sent["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(sent["data"])
    assert body["instance"] == "prod"
    assert body["job"] == "db"
    assert body["snapshot_id"] == "snap-1"
    assert body["status"] == "error"
    assert body["message"] == "disk full"
    assert body["errors"] == ["e1", "e2"]
    assert body["metrics"] == {"records_count": 5, "workflows_count": 2}


def test_webhook_body_is_sorted_keys_bytes():
    cfg = WebhookChannelConfig(url="https://hook.example/endpoint")
    sent, transport = _recorder()
    WebhookChannel(cfg, transport=transport).send(_event())

    assert isinstance(sent["data"], (bytes, bytearray))
    payload = json.loads(sent["data"])
    assert sent["data"] == json.dumps(payload, sort_keys=True).encode("utf-8")


def test_webhook_signature_absent_without_secret():
    cfg = WebhookChannelConfig(url="https://hook.example/endpoint")
    sent, transport = _recorder()
    WebhookChannel(cfg, transport=transport).send(_event())

    assert not any(k.lower() == "x-signature-256" for k in sent["headers"])


def test_webhook_signature_present_and_verifies():
    secret = "s3cr3t-key"
    cfg = WebhookChannelConfig(url="https://hook.example/endpoint", secret=secret)
    sent, transport = _recorder()
    WebhookChannel(cfg, transport=transport).send(_event())

    header = sent["headers"]["X-Signature-256"]
    assert header.startswith("sha256=")
    expected = hmac.new(secret.encode("utf-8"), sent["data"], hashlib.sha256).hexdigest()
    assert header == f"sha256={expected}"


def test_webhook_without_url_raises():
    cfg = WebhookChannelConfig()
    with pytest.raises(ValueError):
        WebhookChannel(cfg, transport=lambda *a, **k: None).send(_event())
