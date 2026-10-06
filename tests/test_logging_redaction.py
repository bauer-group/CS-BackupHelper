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


def test_masks_credential_keys_that_do_not_end_in_a_secret_word():
    # "secret_key" used to slip through: the key only matched when it ENDED in
    # password|secret|token|api_key|access_key.
    for text in (
        '{"secret_key": "S3CR3T-VALUE"}',
        "secret_key=S3CR3T-VALUE",
        "{'secret_key': 'S3CR3T-VALUE'}",          # python repr in an exception
        '{"aws_secret_access_key": "S3CR3T-VALUE"}',
        '{"private_key": "S3CR3T-VALUE"}',
        '{"client_secret": "S3CR3T-VALUE"}',
        '{"sse_customer_key": "S3CR3T-VALUE"}',
        '{"apiKey": "S3CR3T-VALUE"}',
        "SMTP_PASSWORD: S3CR3T-VALUE",
        "ntfy token=S3CR3T-VALUE",
    ):
        assert "S3CR3T-VALUE" not in redact(text), text


def test_masks_a_quoted_secret_containing_an_escaped_quote():
    out = redact('{"password": "abc\\"S3CR3T-TAIL", "host": "db"}')
    assert "S3CR3T-TAIL" not in out
    assert '"host": "db"' in out


def test_does_not_mask_hashes_or_object_keys():
    # Integrity hashes and bare object-store keys are diagnostic, not secret.
    text = '{"sha256": "ab12", "archive_sha256": "cd34", "key": "app/s1.tar.gz"}'
    assert redact(text) == text
    assert redact("key=app/s1.tar.gz") == "key=app/s1.tar.gz"


def test_filter_masks_secret_key_in_log_records():
    f = SecretRedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "s3 config %s", ({"bucket": "b", "secret_key": "S3CR3T-VALUE"},), None)
    assert f.filter(rec) is True
    assert "S3CR3T-VALUE" not in rec.getMessage()
    assert "'bucket': 'b'" in rec.getMessage()


def test_redact_data_masks_sensitive_values_structurally():
    from backuphelper.logging_setup import redact_data

    data = {"destinations": [{"type": "s3", "bucket": "b", "secret_key": "S3CR3T",
                              "access_key": "AKIA", "endpoint": "https://u:pw@minio:9000"}],
            "email": {"password": "", "username": "ops"},
            "webhook": {"secret": None},
            "source": {"type": "custom", "port": 5432, "passphrase": 1234}}
    out = redact_data(data)
    s3 = out["destinations"][0]
    assert s3["secret_key"] == "***" and s3["access_key"] == "***"
    assert s3["bucket"] == "b" and s3["type"] == "s3"
    assert "pw" not in s3["endpoint"] and "minio" in s3["endpoint"]
    assert out["email"] == {"password": "", "username": "ops"}  # unset stays visible
    assert out["webhook"] == {"secret": None}
    assert out["source"]["passphrase"] == "***" and out["source"]["port"] == 5432
    assert data["destinations"][0]["secret_key"] == "S3CR3T"  # input not mutated


def test_masks_a_pair_nested_in_a_non_secret_value():
    # The scanner must not swallow a whole "error: ..." value and skip the
    # credential pair inside it.
    assert "hunter2" not in redact("error: password=hunter2 rejected")
    out = redact("url=https://h/wf?api-version=1&sig=TEAMS-SIG")
    assert "TEAMS-SIG" not in out and out.startswith("url=https://h/wf?api-version=1&sig=")
    out = redact("GET https://b/k?X-Amz-Credential=AKIA%2F1&X-Amz-Signature=abc ok")
    assert "AKIA" not in out and out.endswith(" ok")


def test_redaction_is_linear_on_long_lines():
    # The old pattern backtracked over every [a-z0-9_.-] run (a 4000-char
    # token-dense line took minutes); the filter runs on every log record.
    import time

    for line in ("tokenx" * 11000, "a" * 64000, "a=" * 32000, 'password="' + "a " * 30000):
        started = time.perf_counter()
        redact(line)
        assert time.perf_counter() - started < 1.0, line[:20]


def test_redact_data_masks_notification_urls_and_the_ntfy_topic():
    from backuphelper.logging_setup import redact_data

    notifications = {
        "slack": {"url": "https://hooks.slack.com/services/T0/B0/SLACK-SECRET"},
        "teams": {"url": "https://prod.logic.azure.com:443/workflows/x?sp=1&sig=TEAMS-SIG"},
        "healthchecks": {"url": "https://hc-ping.com/HC-UUID"},
        "ntfy": {"url": "https://ntfy.sh", "topic": "NTFY-TOPIC", "token": None},
        "discord": {"url": ""},
    }
    out = redact_data({"jobs": [{"notifications": notifications,
                                 "destinations": [{"endpoint": "https://minio:9000"}]}]})
    job = out["jobs"][0]
    got = job["notifications"]
    assert got["slack"]["url"] == "https://hooks.slack.com/***"
    assert got["teams"]["url"] == "https://prod.logic.azure.com:443/***"
    assert got["healthchecks"]["url"] == "https://hc-ping.com/***"
    assert got["ntfy"] == {"url": "https://ntfy.sh", "topic": "***", "token": None}
    assert got["discord"]["url"] == ""                         # unset stays visible
    assert job["destinations"][0]["endpoint"] == "https://minio:9000"  # not a webhook


def test_filter_masks_the_traceback_of_a_logged_exception():
    import io
    import json as _json

    from backuphelper.logging_setup import _JsonFormatter

    for formatter in (logging.Formatter("%(message)s"), _JsonFormatter()):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SecretRedactingFilter())
        handler.setFormatter(formatter)
        logger = logging.getLogger(f"redaction-test-{type(formatter).__name__}")
        logger.addHandler(handler)
        try:
            raise RuntimeError("connect failed: postgres://app:S3CR3T@db/app password=S3CR3T")
        except RuntimeError:
            logger.exception("scheduled run failed")
        finally:
            logger.removeHandler(handler)
        out = stream.getvalue()
        assert "S3CR3T" not in out and "RuntimeError" in out
        if isinstance(formatter, _JsonFormatter):
            assert "Traceback" in _json.loads(out)["exc"]
