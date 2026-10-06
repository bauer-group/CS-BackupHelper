"""Logging: console/JSON formatting + a global secret-redacting filter.

The redaction filter is a defence-in-depth measure: even if a secret slips into
a log call, key=value pairs and DSN-embedded credentials are masked before the
line is emitted. The same key rule drives :func:`redact_data`, which masks a
parsed config structure (the ``config`` command) instead of regex-patching text.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any
from urllib.parse import urlsplit

_MASK = "***"

# A key name holds a credential (case-insensitive) when it
#   * contains password / passwd / passphrase / secret / token / credential /
#     signature, or an api / access / private key — secret_key,
#     aws_secret_access_key, client_secret, smtp_password, webhook "secret",
#     ntfy "token", X-Amz-Credential, X-Amz-Signature, privateKeyPem, ...
#   * ends in a qualified "key" (``_key`` / ``-key`` / ``.key``) —
#     sse_customer_key, encryption-key, ...
#   * is exactly ``sig`` (the signature parameter of a Teams workflow URL).
# A bare "key" is deliberately NOT a secret: throughout the engine it names an
# object-store key (an S3 object path), which is diagnostic. Hashes ("sha256",
# "archive_sha256") are integrity data and never match either.
_SECRET_WORD = re.compile(
    r"password|passwd|passphrase|secret|token|credential|signature"
    r"|(?:api|access|private)[_.-]?key"
)

# A key=value / key: value pair, the key optionally quoted (JSON, python repr).
# The key is anchored at the start of a name run and matched possessively, so
# scanning a line is linear in its length (no backtracking over long runs);
# whether the key is sensitive is decided in code, by is_sensitive_key().
_KEY = re.compile(r"""(?<![a-z0-9_.-])(["']?)([a-z0-9_.-]++)\1\s*+[=:]\s*+""", re.IGNORECASE)
# The value: a quoted string honouring backslash escapes (so a secret that
# contains a quote cannot leak its tail), else everything up to whitespace.
_VALUE = re.compile(r""""(?:\\.|[^"\\])*+"|'(?:\\.|[^'\\])*+'|\S+""")
# scheme://user:PASSWORD@host  → mask the password segment only.
_DSN = re.compile(r"(://[^:/@\s]++:)([^@/\s]++)(@)")


def is_sensitive_key(name: str) -> bool:
    """Whether a config/log key name holds a credential (rule above)."""
    lowered = name.lower()
    return (_SECRET_WORD.search(lowered) is not None
            or lowered.endswith(("_key", "-key", ".key"))
            or lowered == "sig")


def _mask_pairs(text: str) -> str:
    out: list[str] = []
    pos = 0
    while (pair := _KEY.search(text, pos)) is not None:
        value = _VALUE.match(text, pair.end()) if is_sensitive_key(pair.group(2)) else None
        if value is None:
            # Not a credential: keep the key and keep scanning INSIDE its value,
            # so "error: password=x" or "url=https://h/?token=x" still masks.
            out.append(text[pos:pair.end()])
            pos = pair.end()
            continue
        raw = value.group(0)
        quote = raw[0] if len(raw) > 1 and raw[0] in "\"'" and raw[-1] == raw[0] else ""
        out.append(f"{text[pos:pair.end()]}{quote}{_MASK}{quote}")
        pos = value.end()
    out.append(text[pos:])
    return "".join(out)


def redact(text: str) -> str:
    text = _mask_pairs(text)
    return _DSN.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(3)}", text)


def redact_data(data: Any, _path: tuple[str, ...] = ()) -> Any:
    """Return a copy of a JSON-like structure with every credential masked.

    The value of a sensitive key is replaced wholesale; an unset value (``None``,
    ``""``) or a boolean is kept, since it carries no secret and "is it set?" is
    exactly what an operator inspects. A notification channel's ``url`` is cut
    down to ``scheme://host/***`` and an ntfy ``topic`` is masked, because there
    the address itself is the credential (Slack/Discord webhook path, Teams
    ``sig=``, healthchecks check UUID, a public ntfy topic). Every other string
    goes through :func:`redact`, so DSN- or query-embedded credentials are masked
    too. Working on the parsed structure keeps the output valid JSON.
    """
    if isinstance(data, dict):
        return {k: _redact_entry(_path + (str(k),), v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_data(v, _path) for v in data]
    if isinstance(data, str):
        return redact(data)
    return data


def _redact_entry(path: tuple[str, ...], value: Any) -> Any:
    key = path[-1]
    if value is None or value == "" or isinstance(value, bool):
        return value
    if is_sensitive_key(key):
        return _MASK
    if "notifications" in path[:-1] and isinstance(value, str):
        if key == "url":
            return _mask_url(value)
        if key == "topic":
            return _MASK
    return redact_data(value, path)


def _mask_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]  # never echo user:password@
    if not (parts.scheme and host):
        return _MASK
    hidden = parts.path.strip("/") or parts.query or parts.fragment
    return f"{parts.scheme}://{host}/{_MASK}" if hidden else f"{parts.scheme}://{host}"


class SecretRedactingFilter(logging.Filter):
    _traceback = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the run
            return True
        record.msg = redact(message)
        record.args = ()
        if record.exc_info and not record.exc_text:
            # Formatters reuse exc_text, so a traceback (whose last line is the
            # exception message, e.g. a DSN or "password=...") is masked too.
            record.exc_text = redact(self._traceback.formatException(record.exc_info))
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = record.exc_text or self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str = "INFO", fmt: str = "console") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SecretRedactingFilter())
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)
    for noisy in ("botocore", "boto3", "urllib3", "apscheduler", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
