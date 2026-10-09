"""The email channel against a real SMTP server on a loopback socket.

The fakes in test_email.py prove which SMTP class and options the channel
uses; these tests prove the wire behaviour: an SMTPS session is encrypted from
the first byte, never issues STARTTLS, and refuses a server whose certificate
it cannot verify. The server certificate is generated at runtime (no key
material in the repository) with ``cryptography``, which the test extra
already installs through moto.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backuphelper.config.models import EmailChannelConfig
from backuphelper.notify import email as email_module
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.email import EmailChannel

x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

LOOPBACK = "127.0.0.1"


@pytest.fixture(autouse=True)
def _short_smtp_timeout(monkeypatch):
    # A broken test server fails the test in seconds, not after the 60 s bound.
    monkeypatch.setattr(email_module, "SMTP_TIMEOUT_SECONDS", 10)


@pytest.fixture(scope="module")
def server_cert(tmp_path_factory):
    """A self-signed certificate for 127.0.0.1 and its key, as PEM files."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, LOOPBACK)])
    now = datetime.now(timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(LOOPBACK))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("smtps")
    cert_file = directory / "server.pem"
    key_file = directory / "server.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


class SmtpsServer(threading.Thread):
    """Serves one SMTP session that is TLS from the first byte (port 465 style)
    and records the commands and the message it received."""

    def __init__(self, cert_file, key_file):
        super().__init__(daemon=True)
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(cert_file, key_file)
        self._listener = socket.create_server((LOOPBACK, 0))
        self._listener.settimeout(10)
        self.port = self._listener.getsockname()[1]
        self.tls_version: str | None = None
        self.commands: list[str] = []
        self.message = b""
        self.error: Exception | None = None

    def run(self):
        try:
            raw, _ = self._listener.accept()
            raw.settimeout(10)
            with self._tls.wrap_socket(raw, server_side=True) as conn:
                self.tls_version = conn.version()
                self._converse(conn.makefile("rwb"))
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            self.error = exc
        finally:
            self._listener.close()

    def _converse(self, stream):
        def reply(line: str) -> None:
            stream.write(line.encode("ascii") + b"\r\n")
            stream.flush()

        reply("220 smtps.test ESMTP")
        while line := stream.readline():
            verb = line.decode("ascii").split(" ", 1)[0].strip().upper()
            self.commands.append(verb)
            if verb == "DATA":
                reply("354 end with <CRLF>.<CRLF>")
                while (data := stream.readline()) not in (b".\r\n", b""):
                    self.message += data
                reply("250 queued")
            elif verb == "QUIT":
                reply("221 bye")
                return
            else:
                reply("250 smtps.test")


def _cfg(port, **overrides):
    base = dict(
        host=LOOPBACK,
        port=port,
        implicit_tls=True,
        sender="backups@example.com",
        recipients=["ops@example.com"],
    )
    base.update(overrides)
    return EmailChannelConfig(**base)


def _event():
    return AlertEvent(
        status="error",
        title="backup error",
        message="snapshot is incomplete - a component failed",
        instance="prod",
        snapshot_id="2026-10-09_03-15-00",
        errors=["db: pg_dump exited 1"],
    )


def test_implicit_tls_delivers_over_tls_from_the_first_byte(server_cert, monkeypatch):
    cert_file, key_file = server_cert
    # Trust the test certificate the way an operator's CA bundle would: the
    # channel builds its own default (verifying) context, which reads this.
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
    server = SmtpsServer(cert_file, key_file)
    server.start()

    EmailChannel(_cfg(server.port)).send(_event())
    server.join(10)

    assert server.error is None
    assert server.tls_version and server.tls_version.startswith("TLS")
    assert "STARTTLS" not in server.commands  # tls (default true) is not used
    assert server.commands[-1] == "QUIT"
    assert b"Subject: [prod] backup error: 2026-10-09_03-15-00" in server.message
    assert b"db: pg_dump exited 1" in server.message


def test_implicit_tls_refuses_a_server_it_cannot_verify(server_cert, monkeypatch):
    cert_file, key_file = server_cert
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)  # the self-signed cert is not trusted
    server = SmtpsServer(cert_file, key_file)
    server.start()

    with pytest.raises(ssl.SSLCertVerificationError):
        EmailChannel(_cfg(server.port)).send(_event())
    server.join(10)

    assert server.commands == []  # nothing, credentials included, crossed the wire
    assert server.message == b""
