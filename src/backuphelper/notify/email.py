"""Email channel: a multipart text+HTML message sent over SMTP.

The SMTP classes are injectable (defaulting to :class:`smtplib.SMTP` and, for
implicit TLS, :class:`smtplib.SMTP_SSL`) so tests can substitute a recorder and
assert on the built message and recipients without ever opening a socket.
STARTTLS and authentication are applied only when the config asks for them.

Connection security, by config:

* ``implicit_tls: true`` - TLS from the first byte (SMTPS, port 465). The
  server certificate and host name are verified against the system CA store;
  ``tls`` is not used, as the session is already encrypted.
* ``tls: true`` (the default) - a plain connection upgraded with STARTTLS,
  using smtplib's default context exactly as before implicit TLS existed.
* ``tls: false`` - plain SMTP, for an internal relay without TLS.

Every socket operation of the SMTP session is bounded by
:data:`SMTP_TIMEOUT_SECONDS`. Without it a server that never answers - an
SMTPS port waiting for a TLS handshake the client never starts, a stalled
relay - would block the run that sends the alert, and with it every later
scheduled run of the job, indefinitely.

Every value interpolated into the HTML part is escaped: error texts carry run
data (file names, database and exception messages) that must reach the
recipient as text and never be interpreted as markup by the mail client. The
plain-text part needs no escaping and carries the same values verbatim. Both
parts show the same run figures (duration, size); the HTML part keeps the line
breaks of a multi-line error text (``white-space: pre-wrap``).
"""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable, ClassVar

from backuphelper.config.models import EmailChannelConfig
from backuphelper.notify.base import (
    AlertEvent,
    Channel,
    ChannelNotConfigured,
    format_summary,
)

SmtpFactory = Callable[..., smtplib.SMTP]

# Upper bound for every blocking step of the SMTP session (connect, greeting,
# each command, the message upload). Generous for a submission server, finite
# so an unresponsive one fails the channel instead of hanging the run.
SMTP_TIMEOUT_SECONDS = 60


def _esc(value: object) -> str:
    """``value`` as HTML text, safe in element content and quoted attributes."""
    return html.escape(str(value), quote=True)


def _run_figures(event: AlertEvent) -> list[tuple[str, str]]:
    """Duration and size of the run, each only when the event carries it -
    one source for both message parts, so they always show the same."""
    figures: list[tuple[str, str]] = []
    if event.duration_seconds:
        figures.append(("Duration", f"{event.duration_seconds:.1f}s"))
    if event.total_bytes:
        figures.append(("Size", f"{event.total_bytes} bytes"))
    return figures


class EmailChannel(Channel):
    """Sends backup alerts as email."""

    name: ClassVar[str] = "email"

    def __init__(
        self,
        cfg: EmailChannelConfig,
        *,
        smtp_factory: SmtpFactory = smtplib.SMTP,
        smtps_factory: SmtpFactory = smtplib.SMTP_SSL,
    ):
        self.cfg = cfg
        self._smtp_factory = smtp_factory
        self._smtps_factory = smtps_factory

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.host:
            raise ChannelNotConfigured("email channel requires a host")
        if not self.cfg.recipients:
            # Empty entries are already dropped by the config model, so [""] from an
            # unset ALERT_EMAIL-style variable lands here instead of in an SMTP RCPT.
            raise ChannelNotConfigured("email channel has no recipient address")

        msg = self._build_message(event)

        with self._connect() as smtp:
            if self.cfg.tls and not self.cfg.implicit_tls:
                smtp.starttls()
            if self.cfg.username and self.cfg.password:
                smtp.login(self.cfg.username, self.cfg.password)
            smtp.send_message(msg)

    def _connect(self) -> smtplib.SMTP:
        """Open the SMTP session: TLS from the first byte with ``implicit_tls``,
        otherwise a plain connection that STARTTLS may upgrade."""
        if self.cfg.implicit_tls:
            # smtplib's own SMTP_SSL default context checks neither the chain
            # nor the host name; a default context checks both.
            return self._smtps_factory(
                self.cfg.host,
                self.cfg.port,
                timeout=SMTP_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        return self._smtp_factory(self.cfg.host, self.cfg.port, timeout=SMTP_TIMEOUT_SECONDS)

    def _build_message(self, event: AlertEvent) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[{event.instance}] backup {event.status}: {event.snapshot_id}"
        msg["From"] = self.cfg.sender or ""
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content(self._text_body(event))
        msg.add_alternative(self._html_body(event), subtype="html")
        return msg

    def _text_body(self, event: AlertEvent) -> str:
        lines = [format_summary(event), ""]
        if event.job:
            lines.append(f"Job: {event.job}")
        lines.extend(f"{label}: {value}" for label, value in _run_figures(event))
        if event.errors:
            lines.append("")
            lines.append("Errors:")
            lines.extend(f"  - {e}" for e in event.errors)
        return "\n".join(lines) + "\n"

    def _html_body(self, event: AlertEvent) -> str:
        errors_html = ""
        if event.errors:
            # pre-wrap keeps the line breaks and indentation of a multi-line
            # error (a pg_dump stderr, a traceback) and still wraps long lines.
            items = "".join(
                f'<li style="white-space:pre-wrap">{_esc(e)}</li>' for e in event.errors
            )
            errors_html = f"<h3>Errors</h3><ul>{items}</ul>"
        figures = "".join(
            f"<br><strong>{label}:</strong> {_esc(value)}"
            for label, value in _run_figures(event)
        )
        return (
            f"<html><body>"
            f"<h2>{_esc(event.title)}</h2>"
            f"<p>{_esc(event.message)}</p>"
            f"<p><strong>Instance:</strong> {_esc(event.instance)}<br>"
            f"<strong>Job:</strong> {_esc(event.job)}<br>"
            f"<strong>Snapshot:</strong> {_esc(event.snapshot_id)}<br>"
            f"<strong>Status:</strong> {_esc(event.status)}{figures}</p>"
            f"{errors_html}"
            f"</body></html>"
        )
