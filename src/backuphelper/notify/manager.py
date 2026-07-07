"""The AlertManager: severity gating plus fault-isolated fan-out.

``notify`` first decides whether an event clears the configured severity level,
then builds only the named channels and delivers to each. Delivery is wrapped
per channel so a single misconfigured or failing channel is logged and skipped
rather than aborting the whole notification — a backup alert must reach every
*working* channel even when one is broken.
"""

from __future__ import annotations

import logging
from typing import ClassVar, Type

from backuphelper.config.models import NotifyConfig
from backuphelper.notify.base import AlertEvent, Channel
from backuphelper.notify.discord import DiscordChannel
from backuphelper.notify.email import EmailChannel
from backuphelper.notify.healthchecks import HealthchecksChannel
from backuphelper.notify.ntfy import NtfyChannel
from backuphelper.notify.slack import SlackChannel
from backuphelper.notify.teams import TeamsChannel
from backuphelper.notify.webhook import WebhookChannel

logger = logging.getLogger(__name__)

# Config channel name -> Channel implementation.
CHANNELS: dict[str, Type[Channel]] = {
    "email": EmailChannel,
    "webhook": WebhookChannel,
    "teams": TeamsChannel,
    "slack": SlackChannel,
    "discord": DiscordChannel,
    "ntfy": NtfyChannel,
    "healthchecks": HealthchecksChannel,
}

# Statuses that clear each severity level.
_LEVEL_STATUSES: dict[str, frozenset[str]] = {
    "errors": frozenset({"error"}),
    "warnings": frozenset({"warning", "error"}),
    "all": frozenset({"success", "warning", "error"}),
}


class AlertManager:
    """Gates alert events by severity and fans them out to configured channels."""

    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def notify(self, event: AlertEvent) -> None:
        if not self.cfg.channels:
            return
        if not self._passes_level(event.status):
            return

        for name in self.cfg.channels:
            self._deliver(name, event)

    def _passes_level(self, status: str) -> bool:
        allowed = _LEVEL_STATUSES.get(self.cfg.level, _LEVEL_STATUSES["warnings"])
        return status in allowed

    def _deliver(self, name: str, event: AlertEvent) -> None:
        channel_cls = CHANNELS.get(name)
        if channel_cls is None:
            logger.warning("unknown notification channel %r; skipping", name)
            return
        try:
            channel = channel_cls(getattr(self.cfg, name))
            channel.send(event)
        except Exception:  # noqa: BLE001 - per-channel fault isolation
            logger.exception("notification channel %r failed", name)
