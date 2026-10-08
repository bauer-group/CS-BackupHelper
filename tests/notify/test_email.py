"""Tests for the email channel (multipart text+HTML via an injected SMTP)."""

from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser

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


# ------------------------------------------------- recipient normalization ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ([""], []),                                   # unset ALERT_EMAIL in a stack
        (["   "], []),
        (["a@x.com, b@y.com"], ["a@x.com", "b@y.com"]),  # one CSV element
        (["a@x.com; b@y.com", " c@z.com "], ["a@x.com", "b@y.com", "c@z.com"]),
        (["a@x.com", "", "b@y.com,"], ["a@x.com", "b@y.com"]),
        ("a@x.com,b@y.com", ["a@x.com", "b@y.com"]),  # a plain string
        ("", []),
    ],
)
def test_recipients_are_split_stripped_and_empty_entries_dropped(raw, expected):
    assert EmailChannelConfig(recipients=raw).recipients == expected


def test_email_with_only_empty_recipients_is_not_configured_and_never_connects():
    from backuphelper.notify.base import ChannelNotConfigured

    created: list = []
    with pytest.raises(ChannelNotConfigured):
        EmailChannel(_cfg(recipients=[""]), smtp_factory=_factory(created)).send(_event())
    assert created == []  # no SMTP session, so no RCPT with an empty address


# ------------------------------------------------------------ HTML escaping ---

# Markup an error text can carry in practice: a crafted file name below a
# filesystem source, a database error quoting its input, an exception repr.
# It holds a script, an element with an event-handler attribute, a character
# reference and both quote characters.
MARKUP = "<script>alert(1)</script><img src=x onerror=\"alert(2)\">&copy; 'q'"


class _HtmlProbe(HTMLParser):
    """Reads an HTML body like a renderer: the elements it would build (start
    tags) and the decoded text a recipient sees."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self._text: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self._text.append(data)

    @property
    def text(self) -> str:
        return "".join(self._text)


def _sent(event):
    created: list = []
    EmailChannel(_cfg(), smtp_factory=_factory(created)).send(event)
    return created[0].sent_messages[0]


def _html_part(event) -> str:
    return _sent(event).get_body(preferencelist=("html",)).get_content()


def _text_part(event) -> str:
    return _sent(event).get_body(preferencelist=("plain",)).get_content()


def test_email_html_shows_markup_in_error_texts_as_text():
    error = f"files: incomplete, skipped: /data/uploads/{MARKUP}.jpg"
    event = replace(_event(), errors=[error, "db: pg_dump exited 1"])

    probe = _HtmlProbe(_html_part(event))

    assert "script" not in probe.tags
    assert "img" not in probe.tags
    assert probe.tags.count("li") == 2  # one item per error, none injected
    assert error in probe.text  # the recipient reads the error verbatim


def test_email_html_markup_in_errors_does_not_change_the_document_structure():
    plain = replace(_event(), errors=["first error", "second error"])
    crafted = replace(_event(), errors=["</li></ul><h1>forged</h1><ul><li>", MARKUP])

    assert _HtmlProbe(_html_part(crafted)).tags == _HtmlProbe(_html_part(plain)).tags


@pytest.mark.parametrize(
    "field", ["title", "message", "instance", "job", "snapshot_id", "status"]
)
def test_email_html_escapes_every_interpolated_field(field):
    event = replace(_event(), **{field: MARKUP})

    probe = _HtmlProbe(_html_part(event))

    assert "script" not in probe.tags
    assert "img" not in probe.tags
    assert MARKUP in probe.text


def test_email_text_part_keeps_error_texts_verbatim():
    event = replace(_event(), errors=[MARKUP])

    text = _text_part(event)

    assert f"  - {MARKUP}" in text
    assert "&lt;" not in text and "&amp;" not in text
