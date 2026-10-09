"""Tests for the shared S3 client: TLS certificate verification of the endpoint.

The verification tests run real boto3 calls against a local HTTPS server whose
certificate is signed by a throw-away CA, so they exercise the TLS handshake
itself, not just the arguments handed to boto3.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from botocore.exceptions import SSLError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backuphelper.destinations.s3 import S3Destination
from backuphelper.net import s3 as s3net
from backuphelper.net.s3 import S3ConnectionConfig, tls_verify
from backuphelper.sources.s3_bucket import S3BucketSource


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_pki(directory: Path) -> tuple[Path, Path, Path]:
    """A private CA and a server certificate for 127.0.0.1 signed by it."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder()
          .subject_name(_name("backuphelper test CA")).issuer_name(_name("backuphelper test CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(_name("127.0.0.1")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_pem, cert_pem, key_pem = directory / "ca.pem", directory / "server.pem", directory / "server.key"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return ca_pem, cert_pem, key_pem


class _BucketExists(BaseHTTPRequestHandler):
    """Answers every HEAD (head_bucket) with 200, like an existing bucket."""

    def do_HEAD(self):  # noqa: N802 - http.server naming
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def https_endpoint(tmp_path, monkeypatch):
    """(endpoint URL, CA bundle path) of a local HTTPS server with a private CA."""
    # One attempt: a failed handshake must fail the test now, not after retries.
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    ca_pem, cert_pem, key_pem = _write_pki(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BucketExists)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_pem, key_pem)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", ca_pem
    finally:
        server.shutdown()
        server.server_close()


def _dest(endpoint: str, **tls) -> dict:
    return {"type": "s3", "endpoint": endpoint, "bucket": "offsite", "access_key": "k",
            "secret_key": "s", "ensure_bucket": True, **tls}


def test_verification_is_on_by_default(https_endpoint):
    endpoint, _ca = https_endpoint
    # ensure_bucket's head_bucket is the first request: an untrusted CA fails it.
    with pytest.raises(SSLError):
        S3Destination(_dest(endpoint))


def test_ca_bundle_trusts_a_private_ca(https_endpoint):
    endpoint, ca = https_endpoint
    S3Destination(_dest(endpoint, ca_bundle=str(ca)))  # head_bucket succeeds


@pytest.mark.filterwarnings("ignore::urllib3.exceptions.InsecureRequestWarning")
def test_verification_can_be_switched_off_with_a_warning(https_endpoint, caplog):
    endpoint, _ca = https_endpoint
    with caplog.at_level(logging.WARNING, logger="backuphelper.net.s3"):
        S3Destination(_dest(endpoint, verify_tls=False))
    assert any("verification is DISABLED" in r.getMessage() and endpoint in r.getMessage()
               for r in caplog.records)


def test_default_leaves_the_choice_to_boto3():
    # None (not True) keeps boto3's own default, which honours AWS_CA_BUNDLE:
    # a deployment that sets none of the new options behaves as before.
    assert tls_verify(S3ConnectionConfig()) is None


def test_missing_ca_bundle_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        tls_verify(S3ConnectionConfig(ca_bundle=str(tmp_path / "missing.pem")))


def test_verify_tls_accepts_an_interpolated_string():
    # BACKUP_CONFIG_JSON in compose renders "${S3_VERIFY_TLS}" as a string.
    assert S3ConnectionConfig.model_validate({"verify_tls": "false"}).verify_tls is False
    assert S3ConnectionConfig.model_validate({"verify_tls": "true"}).verify_tls is True


@pytest.mark.parametrize("tls,expected", [
    ({}, None),
    ({"verify_tls": False}, False),
])
def test_source_and_destination_pass_the_setting_to_boto3(monkeypatch, tls, expected):
    seen: list = []

    def fake_client(*_args, **kwargs):
        seen.append(kwargs["verify"])
        return object()

    monkeypatch.setattr(s3net.boto3, "client", fake_client)
    S3BucketSource({"type": "s3", "bucket": "assets", **tls})
    S3Destination({"type": "s3", "bucket": "offsite", "ensure_bucket": False, **tls})
    assert seen == [expected, expected]


def test_source_and_destination_pass_a_ca_bundle_to_boto3(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    seen: list = []
    monkeypatch.setattr(s3net.boto3, "client",
                        lambda *_a, **kw: seen.append(kw["verify"]) or object())
    S3BucketSource({"type": "s3", "bucket": "assets", "ca_bundle": str(ca)})
    S3Destination({"type": "s3", "bucket": "offsite", "ensure_bucket": False,
                   "ca_bundle": str(ca)})
    assert seen == [str(ca), str(ca)]
