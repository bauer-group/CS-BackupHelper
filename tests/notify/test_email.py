"""Tests for the email channel (multipart text+HTML via an injected SMTP)."""

from __future__ import annotations

import pytest

from backuphelper.config.models import EmailChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.email import EmailChannel


class FakeSMTP:
    """Records SMTP interactions instead of opening a socket."""

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls_started = False
        self.login_args = None
        self.sent_messages = []
        self.quit_called = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls_started = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg):
        self.sent_messages.append(msg)

    def quit(self):
        self.quit_called = True


def _factory(bucket):
    def factory(host, port, timeout=None):
        smtp = FakeSMTP(host, port, timeout)
        bucket.append(smtp)
        return smtp

    return factory


def _cfg(**overrides):
    base = dict(
        host="smtp.example.com",
        port=587,
        tls=True,
        username="user",
        password="pass",
        sender="backups@example.com",
        recipients=["ops@example.com", "oncall@example.com"],
    )
    base.update(overrides)
    return EmailChannelConfig(**base)


def _event(status="error"):
    return AlertEvent(
        status=status,
        title="Backup failed",
        message="db dump errored",
        instance="prod",
        snapshot_id="snap-1",
    )


def test_email_subject_uses_instance_status_snapshot():
    created: list = []
    EmailChannel(_cfg(), smtp_factory=_factory(created)).send(_event())
    msg = created[0].sent_messages[0]
    assert msg["Subject"] == "[prod] backup error: snap-1"


def test_email_sets_sender_and_recipients():
    created: list = []
    EmailChannel(_cfg(), smtp_factory=_factory(created)).send(_event())
    msg = created[0].sent_messages[0]
    assert msg["From"] == "backups@example.com"
    assert "ops@example.com" in msg["To"]
    assert "oncall@example.com" in msg["To"]


def test_email_is_multipart_text_and_html():
    created: list = []
    EmailChannel(_cfg(), smtp_factory=_factory(created)).send(_event())
    msg = created[0].sent_messages[0]
    assert msg.is_multipart()
    subtypes = {part.get_content_type() for part in msg.walk()}
    assert "text/plain" in subtypes
    assert "text/html" in subtypes


def test_email_connects_to_configured_host_and_port():
    created: list = []
    EmailChannel(_cfg(host="mail.internal", port=2525), smtp_factory=_factory(created)).send(
        _event()
    )
    assert created[0].host == "mail.internal"
    assert created[0].port == 2525


def test_email_starttls_when_tls_enabled():
    created: list = []
    EmailChannel(_cfg(tls=True), smtp_factory=_factory(created)).send(_event())
    assert created[0].tls_started is True


def test_email_no_starttls_when_tls_disabled():
    created: list = []
    EmailChannel(_cfg(tls=False), smtp_factory=_factory(created)).send(_event())
    assert created[0].tls_started is False


def test_email_login_only_when_credentials_present():
    created: list = []
    EmailChannel(_cfg(), smtp_factory=_factory(created)).send(_event())
    assert created[0].login_args == ("user", "pass")

    created2: list = []
    EmailChannel(
        _cfg(username=None, password=None), smtp_factory=_factory(created2)
    ).send(_event())
    assert created2[0].login_args is None


def test_email_without_host_raises():
    with pytest.raises(ValueError):
        EmailChannel(_cfg(host=None), smtp_factory=_factory([])).send(_event())


def test_email_without_recipients_raises():
    with pytest.raises(ValueError):
        EmailChannel(_cfg(recipients=[]), smtp_factory=_factory([])).send(_event())
