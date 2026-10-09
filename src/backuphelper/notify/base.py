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

# Upper bound for connecting to an HTTP receiver and for each wait on its
# answer. Without it a receiver that accepts the request and never answers
# (a hanging endpoint or proxy) blocks the run that sends the alert - and with
# it every later scheduled run of the job - indefinitely; the email channel
# has the same kind of bound (email.SMTP_TIMEOUT_SECONDS).
HTTP_TIMEOUT_SECONDS = 30


def http_post(url: str, data: bytes, headers: Mapping[str, str]) -> None:
    """POST ``data`` to ``url`` with ``headers``. Raises on any HTTP/URL error,
    and when the receiver does not answer within :data:`HTTP_TIMEOUT_SECONDS`."""
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method="POST"
    )
    # The URL is the operator's own channel config (bandit B310).
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS):  # nosec B310
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


class ChannelNotConfigured(ValueError):
    """A channel named in ``channels`` lacks its required config (url, host,
    recipients). The manager skips it with a one-line warning instead of an error
    traceback — it is a deployment setting to fix, not a delivery failure."""


class Channel(ABC):
    """Base class for every alert channel. ``name`` is the config discriminator."""

    name: ClassVar[str] = ""

    @abstractmethod
    def send(self, event: AlertEvent) -> None:
        """Deliver ``event`` over this channel. Raise on delivery failure."""
