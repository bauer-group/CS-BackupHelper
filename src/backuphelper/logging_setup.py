"""Logging: console/JSON formatting + a global secret-redacting filter.

The redaction filter is a defence-in-depth measure: even if a secret slips into
a log call, key=value pairs and DSN-embedded credentials are masked before the
line is emitted.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_MASK = "***"

# key=value / key: value where the key name ends in a secret-looking token
# (also matches prefixed names like aws_access_key, db-password).
_KV = re.compile(
    r"(?i)([a-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key))"
    r"(\s*[=:]\s*)(\S+)"
)
# scheme://user:PASSWORD@host  → mask the password segment only.
_DSN = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")
# JSON: "secret_key": "value"  → mask only the value, keep the quotes.
_JSON_KV = re.compile(
    r'(?i)"([a-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key))"'
    r"(\s*:\s*)\"([^\"]*)\""
)


def redact(text: str) -> str:
    text = _JSON_KV.sub(lambda m: f'"{m.group(1)}"{m.group(2)}"{_MASK}"', text)
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", text)
    text = _DSN.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(3)}", text)
    return text


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the run
            return True
        record.msg = redact(message)
        record.args = ()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
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
