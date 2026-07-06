"""Discord channel: an incoming-webhook JSON POST with a ``content`` field."""

from __future__ import annotations

import json
from typing import ClassVar, Optional

from backuphelper.config.models import SimpleUrlChannelConfig
from backuphelper.notify.base import (
    AlertEvent,
    Channel,
    Transport,
    format_summary,
    http_post,
)


class DiscordChannel(Channel):
    """Posts to a Discord webhook URL."""

    name: ClassVar[str] = "discord"

    def __init__(
        self, cfg: SimpleUrlChannelConfig, *, transport: Optional[Transport] = None
    ):
        self.cfg = cfg
        self._transport: Transport = transport or http_post

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.url:
            raise ValueError("discord channel requires a url")
        body = json.dumps({"content": format_summary(event)}).encode("utf-8")
        self._transport(self.cfg.url, body, {"Content-Type": "application/json"})
