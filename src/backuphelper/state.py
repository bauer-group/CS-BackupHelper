"""Run-state records in the data dir — what the healthcheck reads besides the
sidecar manifests.

A manifest in the data dir only describes a snapshot that is still there. It
cannot tell when the last run happened once ``keep_local: false`` (or an
S3-only job) took the snapshot off the volume, nor that a run aborted before it
wrote one. So every run leaves a small record of its outcome, and the daemon
records when it started (the start of the healthcheck's grace for "no backup
yet"):

    <data_dir>/.state/daemon.json        {"started_at"}
    <data_dir>/.state/job-<name>.json    {"job", "snapshot_id", "status",
                                          "started_at", "failed_components"}

Records are written atomically (temp file + rename), so a concurrent
healthcheck never reads half a file. Snapshot listing, retention and ``prune``
only look at top-level ``*.manifest.json`` files, so they never touch
``.state``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATE_DIR = ".state"
_VERSION = 1
_DAEMON_FILE = "daemon.json"
_JOB_PREFIX = "job-"


@dataclass(frozen=True)
class RunRecord:
    """The outcome of one run of one job."""

    job: str
    snapshot_id: str
    status: str  # success | warning | error
    started_at: datetime
    failed_components: tuple[str, ...] = ()


def parse_timestamp(value: str) -> datetime:
    """An ISO 8601 timestamp as an aware datetime (naive values are UTC)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def state_dir(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_DIR


def _job_file(data_dir: Path, job: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", job) or "_"
    return state_dir(data_dir) / f"{_JOB_PREFIX}{safe}.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_run(data_dir: Path, record: RunRecord) -> None:
    """Persist the outcome of a run; replaces the job's previous record."""
    _write_json(_job_file(data_dir, record.job), {
        "version": _VERSION,
        "job": record.job,
        "snapshot_id": record.snapshot_id,
        "status": record.status,
        "started_at": record.started_at.isoformat(),
        "failed_components": list(record.failed_components),
    })


def record_daemon_start(data_dir: Path, now: Optional[datetime] = None) -> None:
    """Persist when the scheduler daemon started (the healthcheck's grace)."""
    now = now or datetime.now(timezone.utc)
    _write_json(state_dir(data_dir) / _DAEMON_FILE,
                {"version": _VERSION, "started_at": now.isoformat()})


def read_runs(data_dir: Path) -> list[RunRecord]:
    """Every job's last run record; unreadable or malformed files are skipped."""
    runs = []
    for path in sorted(state_dir(data_dir).glob(f"{_JOB_PREFIX}*.json")):
        data = _read_json(path)
        try:
            runs.append(RunRecord(
                job=str(data["job"]), snapshot_id=str(data["snapshot_id"]),
                status=str(data["status"]), started_at=parse_timestamp(data["started_at"]),
                failed_components=tuple(str(n) for n in data.get("failed_components") or ())))
        except (TypeError, KeyError, ValueError, AttributeError):
            continue
    return runs


def read_daemon_start(data_dir: Path) -> Optional[datetime]:
    data = _read_json(state_dir(data_dir) / _DAEMON_FILE)
    try:
        return parse_timestamp(data["started_at"])
    except (TypeError, KeyError, ValueError, AttributeError):
        return None
