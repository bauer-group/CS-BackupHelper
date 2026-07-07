"""ntfy channel: POST the message as a plain-text body to ``url``/``topic``.

The title becomes the ntfy notification title header; a bearer token, when
configured, authenticates against private ntfy instances.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from backuphelper.config.models import NtfyChannelConfig
from backuphelper.notify.base import AlertEvent, Channel, Transport, http_post


class NtfyChannel(Channel):
    """Posts to an ntfy topic."""

    name: ClassVar[str] = "ntfy"

    def __init__(
        self, cfg: NtfyChannelConfig, *, transport: Optional[Transport] = None
    ):
        self.cfg = cfg
        self._transport: Transport = transport or http_post

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.url:
            raise ValueError("ntfy channel requires a url")

        url = self.cfg.url
        if self.cfg.topic:
            url = f"{url.rstrip('/')}/{self.cfg.topic}"

        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if event.title:
            headers["Title"] = event.title
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"

        self._transport(url, event.message.encode("utf-8"), headers)
