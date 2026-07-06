"""The shared notification contract: the AlertEvent payload and Channel ABC.

An ``AlertEvent`` is the transport-agnostic description of one backup outcome.
The :class:`~backuphelper.notify.manager.AlertManager` gates events by severity
and fans them out to the configured :class:`Channel` implementations. Each
channel translates the event into its own wire format and raises on failure so
the manager can isolate that failure from the other channels.
"""

from __future__ import annotations

import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, ClassVar, Mapping

# A pluggable HTTP transport. The default hits the network via urllib; tests
# inject a recording stand-in so no real socket is ever opened.
Transport = Callable[[str, bytes, Mapping[str, str]], None]


def http_post(url: str, data: bytes, headers: Mapping[str, str]) -> None:
    """POST ``data`` to ``url`` with ``headers``. Raises on any HTTP/URL error."""
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method="POST"
    )
    with urllib.request.urlopen(request):  # nosec B310 - operator-configured URL
        pass


@dataclass
class AlertEvent:
    """A single backup outcome, ready to be rendered by any channel.

    ``metrics`` carries plugin enrichment (e.g. ``workflows_count``,
    ``records_count``) so channels can surface source-specific detail without
    the core needing to know about it.
    """

    status: str  # "success" | "warning" | "error"
    title: str
    message: str
    instance: str = ""
    snapshot_id: str = ""
    job: str = ""
    duration_seconds: float = 0.0
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def format_summary(event: AlertEvent) -> str:
    """A one-line human summary shared by the plain-text channels."""
    head = f"[{event.instance}] " if event.instance else ""
    line = f"{head}{event.title}: {event.message}".strip()
    if event.snapshot_id:
        line += f" (snapshot {event.snapshot_id})"
    return line


class Channel(ABC):
    """Base class for every alert channel. ``name`` is the config discriminator."""

    name: ClassVar[str] = ""

    @abstractmethod
    def send(self, event: AlertEvent) -> None:
        """Deliver ``event`` over this channel. Raise on delivery failure."""
