"""Functional healthcheck — reports whether backups actually work.

Unhealthy, checked in this order, when:

1. the data dir is missing, or it (or its ``.state`` dir) is not writable by
   the user the check runs as — no run could store anything;
2. the most recent run ended in ``error`` or left a snapshot with a failed
   component — until a newer run without failed components exists;
3. the most recent run is older than the max age;
4. no run is known at all and the daemon started more than the max age ago,
   or never recorded a start: the grace for a fresh daemon is over.

The max age is the job's ``healthcheck_max_age_hours``, else the
``max_age_hours`` the caller passes (``BACKUP_HEALTHCHECK_MAX_AGE_HOURS``).

"The most recent run" is the newest of the per-job run records
(:mod:`backuphelper.state`) and the sidecar manifests in the data dir; on equal
timestamps the run record wins. The record keeps staleness detectable when
``keep_local: false`` leaves no local manifest, and it covers runs that aborted
before they wrote one. Manifests written before 1.7.7 carry no ``status``; a
failed component in them counts all the same.

With at most one configured job, every record and manifest in the data dir
counts, as in 1.7.7. With several jobs, rules 2-4 apply to every job on its
own - its run record and the manifests of its own snapshots
(:meth:`backuphelper.snapshots.SnapshotScope.owns`) - and the check is
unhealthy when any job is, so a newer good run of one job never masks
another job's failure. Records of jobs that are no longer configured are
ignored then.

The check only reads: it writes nothing and opens no network connection.
Process liveness is covered separately by the Docker ``pgrep`` probe.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from .config.models import Job
from .snapshots import LOCAL, pending_owners, scope_for
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
    job: Optional[str] = None  # the job of a run record
    sid: Optional[str] = None  # the snapshot of a manifest (by its file name)


def _manifest_runs(data_dir: Path) -> list[_Run]:
    runs = []
    for path in data_dir.glob("*.manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = parse_timestamp(data["created_at"])
            failed = [str(c.get("name")) for c in data.get("components") or [] if c.get("error")]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        name = path.name[: -len(".manifest.json")]
        sid = data.get("snapshot_id") or name
        failure = None
        if failed:
            failure = f"failed component(s): {', '.join(failed)}"
        elif data.get("status") == "error":
            failure = "status error"
        runs.append(_Run(created, 0, f"snapshot {sid}", failure, sid=name))
    return runs


def _record_runs(data_dir: Path) -> list[_Run]:
    runs = []
    for rec in read_runs(data_dir):
        failure = None
        if rec.status == "error":
            failure = (f"failed component(s): {', '.join(rec.failed_components)}"
                       if rec.failed_components else "the run ended in error")
        runs.append(_Run(rec.started_at, 1, f"snapshot {rec.snapshot_id} (job {rec.job})",
                         failure, job=rec.job))
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


def _judge(runs: list[_Run], max_age_hours: float, now: datetime,
           started: Optional[datetime]) -> Health:
    """Rules 2-4 over one set of runs."""
    max_age = timedelta(hours=max_age_hours)
    limit = f"{max_age_hours:g} h"
    if not runs:
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


def _max_age(job: Job, default: float) -> float:
    return job.healthcheck_max_age_hours if job.healthcheck_max_age_hours is not None else default


def check(data_dir: Path, max_age_hours: float, now: Optional[datetime] = None,
          jobs: Sequence[Job] = ()) -> Health:
    """Judge the data dir. ``jobs`` are the configured jobs: their own max
    ages, and - with several jobs - one verdict per job (module docstring)."""
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)

    problem = _not_writable(data_dir)
    if problem:
        return Health(False, problem)

    runs = _manifest_runs(data_dir) + _record_runs(data_dir)
    started = read_daemon_start(data_dir)
    if len(jobs) <= 1:
        limit = _max_age(jobs[0], max_age_hours) if jobs else max_age_hours
        return _judge(runs, limit, now, started)

    marked = pending_owners(data_dir)
    verdicts = []
    for job in jobs:
        scope = scope_for(jobs, job)
        own = [r for r in runs if (r.job == job.name if r.job is not None
                                   else scope.owns(r.sid or "", LOCAL, marked.get(r.sid or "")))]
        verdicts.append((job.name, _judge(own, _max_age(job, max_age_hours), now, started)))
    failing = [(name, health) for name, health in verdicts if not health.healthy]
    return Health(not failing, "; ".join(f"job {name}: {health.reason}"
                                         for name, health in failing or verdicts))


def is_healthy(data_dir: Path, max_age_hours: float, now: Optional[datetime] = None,
               jobs: Sequence[Job] = ()) -> bool:
    return check(data_dir, max_age_hours, now, jobs).healthy
