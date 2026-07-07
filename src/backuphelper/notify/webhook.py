"""Webhook channel: a deterministic JSON POST, optionally HMAC-SHA256 signed.

The body is serialized with ``sort_keys=True`` so the exact bytes are stable and
reproducible — which is what makes the signature verifiable: when ``cfg.secret``
is set we sign those exact bytes and ship the digest in ``X-Signature-256`` for
the receiver to re-compute.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import ClassVar, Optional

from backuphelper.config.models import WebhookChannelConfig
from backuphelper.notify.base import AlertEvent, Channel, Transport, http_post


class WebhookChannel(Channel):
    """Generic signed webhook."""

    name: ClassVar[str] = "webhook"

    def __init__(self, cfg: WebhookChannelConfig, *, transport: Optional[Transport] = None):
        self.cfg = cfg
        self._transport: Transport = transport or http_post

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.url:
            raise ValueError("webhook channel requires a url")

        payload = {
            "instance": event.instance,
            "job": event.job,
            "snapshot_id": event.snapshot_id,
            "status": event.status,
            "message": event.message,
            "errors": event.errors,
            "metrics": event.metrics,
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.cfg.secret:
            digest = hmac.new(
                self.cfg.secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            headers["X-Signature-256"] = f"sha256={digest}"

        self._transport(self.cfg.url, body, headers)
