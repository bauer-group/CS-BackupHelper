"""Snapshot ids.

An id is the start of its run in UTC, ``%Y-%m-%d_%H-%M-%S``. In a config with
several jobs it also names the job - ``2026-07-05_03-15-00_files-nightly`` -
because jobs share the data dir (and may share an S3 bucket + prefix), and two
of them starting in the same second would otherwise write the same files. A
single-job config keeps the plain timestamp, the only form up to 1.7.7, so its
ids do not change. Both forms sort by time, and every command takes either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from .config.models import Job

TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
_TIMESTAMP_LEN = len("2026-07-05_03-15-00")
_NOT_IN_SLUG = re.compile(r"[^A-Za-z0-9_-]")


def job_slug(name: str) -> str:
    """The job name as an id carries it: every character other than a letter,
    digit, ``-`` or ``_`` becomes ``-``. A ``.`` in particular must go - the
    files of a snapshot are found by the prefix ``<id>.``, and ``<id>.x`` of
    one job must never match the files of a job named ``<name>.x``."""
    return _NOT_IN_SLUG.sub("-", name) or "-"


def new_snapshot_id(now: datetime, job: Optional[str] = None) -> str:
    """The id of a run started at ``now``; with ``job``, the job-scoped form."""
    stamp = now.strftime(TIMESTAMP_FORMAT)
    return f"{stamp}_{job_slug(job)}" if job is not None else stamp


def parse_snapshot_id(sid: str) -> tuple[Optional[datetime], Optional[str]]:
    """``(start, job slug)`` of an id the engine generated; the slug is None for
    a plain id. ``(None, None)`` for any other id (e.g. one a caller chose)."""
    try:
        when = datetime.strptime(sid[:_TIMESTAMP_LEN], TIMESTAMP_FORMAT)
    except ValueError:
        return None, None
    when = when.replace(tzinfo=timezone.utc)
    rest = sid[_TIMESTAMP_LEN:]
    if not rest:
        return when, None
    slug = rest[1:]
    if rest[0] == "_" and slug and not _NOT_IN_SLUG.search(slug):
        return when, slug
    return None, None


@dataclass(frozen=True)
class SnapshotScope:
    """How one job names its snapshots."""

    job: str
    scoped: bool = False  # ids carry the job name (a config with several jobs)

    def new_id(self, now: datetime) -> str:
        return new_snapshot_id(now, self.job if self.scoped else None)


def scope_for(jobs: Sequence[Job], job: Job) -> SnapshotScope:
    """The scope of ``job`` within the configured ``jobs``."""
    return SnapshotScope(job.name, scoped=len(jobs) > 1)
