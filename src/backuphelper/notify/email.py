"""Email channel: a multipart text+HTML message sent over SMTP.

The SMTP class is injectable (defaulting to :class:`smtplib.SMTP`) so tests can
substitute a recorder and assert on the built message and recipients without
ever opening a socket. STARTTLS and authentication are applied only when the
config asks for them.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Callable, ClassVar

from backuphelper.config.models import EmailChannelConfig
from backuphelper.notify.base import AlertEvent, Channel, format_summary

SmtpFactory = Callable[..., smtplib.SMTP]


class EmailChannel(Channel):
    """Sends backup alerts as email."""

    name: ClassVar[str] = "email"

    def __init__(self, cfg: EmailChannelConfig, *, smtp_factory: SmtpFactory = smtplib.SMTP):
        self.cfg = cfg
        self._smtp_factory = smtp_factory

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.host:
            raise ValueError("email channel requires a host")
        if not self.cfg.recipients:
            raise ValueError("email channel requires at least one recipient")

        msg = self._build_message(event)

        with self._smtp_factory(self.cfg.host, self.cfg.port) as smtp:
            if self.cfg.tls:
                smtp.starttls()
            if self.cfg.username and self.cfg.password:
                smtp.login(self.cfg.username, self.cfg.password)
            smtp.send_message(msg)

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
        if event.duration_seconds:
            lines.append(f"Duration: {event.duration_seconds:.1f}s")
        if event.total_bytes:
            lines.append(f"Size: {event.total_bytes} bytes")
        if event.errors:
            lines.append("")
            lines.append("Errors:")
            lines.extend(f"  - {e}" for e in event.errors)
        return "\n".join(lines) + "\n"

    def _html_body(self, event: AlertEvent) -> str:
        errors_html = ""
        if event.errors:
            items = "".join(f"<li>{e}</li>" for e in event.errors)
            errors_html = f"<h3>Errors</h3><ul>{items}</ul>"
        return (
            f"<html><body>"
            f"<h2>{event.title}</h2>"
            f"<p>{event.message}</p>"
            f"<p><strong>Instance:</strong> {event.instance}<br>"
            f"<strong>Job:</strong> {event.job}<br>"
            f"<strong>Snapshot:</strong> {event.snapshot_id}<br>"
            f"<strong>Status:</strong> {event.status}</p>"
            f"{errors_html}"
            f"</body></html>"
        )
