"""Healthchecks channel: a dead-man's-switch ping.

A success/warning outcome pings the base check URL (the switch stays alive); an
error pings the ``/fail`` endpoint so the monitor flips the check red. The event
message rides along as the request body so it shows up in the check's log.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from backuphelper.config.models import SimpleUrlChannelConfig
from backuphelper.notify.base import AlertEvent, Channel, Transport, http_post


class HealthchecksChannel(Channel):
    """Pings a Healthchecks.io-style monitoring check."""

    name: ClassVar[str] = "healthchecks"

    def __init__(
        self, cfg: SimpleUrlChannelConfig, *, transport: Optional[Transport] = None
    ):
        self.cfg = cfg
        self._transport: Transport = transport or http_post

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.url:
            raise ValueError("healthchecks channel requires a url")

        url = self.cfg.url.rstrip("/")
        if event.status == "error":
            url = f"{url}/fail"

        self._transport(url, event.message.encode("utf-8"), {})
