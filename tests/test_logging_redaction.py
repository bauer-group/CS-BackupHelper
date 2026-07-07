"""Tests for the secret-redacting log filter."""

import logging

from backuphelper.logging_setup import SecretRedactingFilter, redact


def test_masks_key_value_secrets():
    assert "hunter2" not in redact("db password=hunter2 ok")
    assert "abc123" not in redact("token: abc123")
    assert "AKIA999" not in redact("aws_access_key=AKIA999")


def test_masks_dsn_embedded_password():
    out = redact("dsn postgres://user:s3cretpw@host:5432/db")
    assert "s3cretpw" not in out
    assert "user" in out and "host" in out  # only the password is masked


def test_masks_quoted_json_secret_values():
    out = redact('{"db_password": "hunter2", "host": "db"}')
    assert "hunter2" not in out
    assert '"host": "db"' in out  # non-secret keys survive


def test_leaves_ordinary_text_untouched():
    assert redact("snapshot 2026-07-06 completed in 3.2s") == "snapshot 2026-07-06 completed in 3.2s"


def test_filter_masks_the_formatted_record_message():
    f = SecretRedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "connecting with password=%s", ("topsecret",), None)
    assert f.filter(rec) is True
    assert "topsecret" not in rec.getMessage()
