"""Functional healthcheck — reports whether backups actually work.

Unhealthy, checked in this order, when:

1. the data dir is missing, or it (or its ``.state`` dir) is not writable by
   the user the check runs as — no run could store anything;
2. the most recent run ended in ``error`` or left a snapshot with a failed
   component — until a newer run without failed components exists;
3. the most recent run is older than ``max_age_hours``;
4. no run is known at all and the daemon started more than ``max_age_hours``
   ago, or never recorded a start: the grace for a fresh daemon is over.

"The most recent run" is the newest of the per-job run records
(:mod:`backuphelper.state`) and the sidecar manifests in the data dir, across
all jobs; on equal timestamps the run record wins. The record keeps staleness
detectable when ``keep_local: false`` leaves no local manifest, and it covers
runs that aborted before they wrote one. Manifests written before 1.7.7 carry
no ``status``; a failed component in them counts all the same.

The check only reads: it writes nothing and opens no network connection.
Process liveness is covered separately by the Docker ``pgrep`` probe.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .state import STATE_DIR, parse_timestamp, read_daemon_start, read_runs


@dataclass(frozen=True)
class Health:
    healthy: bool
    reason: str


@dataclass(frozen=True)
class _Run:
    at: datetime
    rank: int  # tie-break on equal timestamps: a run record (1) beats a manifest (0)
    label: str
    failure: Optional[str]  # why this run counts as failed, None when it did not fail


def _manifest_runs(data_dir: Path) -> list[_Run]:
    runs = []
    for path in data_dir.glob("*.manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = parse_timestamp(data["created_at"])
            failed = [str(c.get("name")) for c in data.get("components") or [] if c.get("error")]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        sid = data.get("snapshot_id") or path.name[: -len(".manifest.json")]
        failure = None
        if failed:
            failure = f"failed component(s): {', '.join(failed)}"
        elif data.get("status") == "error":
            failure = "status error"
        runs.append(_Run(created, 0, f"snapshot {sid}", failure))
    return runs


def _record_runs(data_dir: Path) -> list[_Run]:
    runs = []
    for rec in read_runs(data_dir):
        failure = None
        if rec.status == "error":
            failure = (f"failed component(s): {', '.join(rec.failed_components)}"
                       if rec.failed_components else "the run ended in error")
        runs.append(_Run(rec.started_at, 1, f"snapshot {rec.snapshot_id} (job {rec.job})",
                         failure))
    return runs


def _not_writable(data_dir: Path) -> Optional[str]:
    if not data_dir.is_dir():
        return f"data dir {data_dir} does not exist"
    who = f"uid {os.getuid()}" if hasattr(os, "getuid") else "the current user"
    for path in (data_dir, data_dir / STATE_DIR):
        if path.is_dir() and not os.access(path, os.W_OK | os.X_OK):
            return f"{path} is not writable by {who} - no backup can be stored"
    return None


def _hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f} h"


def _when(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def check(data_dir: Path, max_age_hours: float, now: Optional[datetime] = None) -> Health:
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    max_age = timedelta(hours=max_age_hours)
    limit = f"{max_age_hours:g} h"

    problem = _not_writable(data_dir)
    if problem:
        return Health(False, problem)

    runs = _manifest_runs(data_dir) + _record_runs(data_dir)
    if not runs:
        started = read_daemon_start(data_dir)
        if started is None:
            return Health(False, "no backup has run yet and no daemon start is recorded")
        if now - started <= max_age:
            return Health(True, f"no backup has run yet - within the {limit} grace after "
                                f"the daemon start at {_when(started)}")
        return Health(False, f"no backup has run in the {_hours(now - started)} since the "
                             f"daemon started at {_when(started)} (limit {limit})")

    latest = max(runs, key=lambda r: (r.at, r.rank))
    age = now - latest.at
    if latest.failure:
        return Health(False, f"the last backup failed: {latest.label} at "
                             f"{_when(latest.at)}: {latest.failure}")
    if age > max_age:
        return Health(False, f"the last backup is stale: {latest.label} ran "
                             f"{_hours(age)} ago (limit {limit})")
    return Health(True, f"the last backup is fresh: {latest.label} ran {_hours(age)} ago")


def is_healthy(data_dir: Path, max_age_hours: float, now: Optional[datetime] = None) -> bool:
    return check(data_dir, max_age_hours, now).healthy
