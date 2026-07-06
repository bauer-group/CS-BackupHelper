"""Notification package: severity-gated fan-out to pluggable alert channels."""

from __future__ import annotations

from backuphelper.notify.base import AlertEvent, Channel
from backuphelper.notify.manager import AlertManager

__all__ = ["AlertEvent", "Channel", "AlertManager"]
