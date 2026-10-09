"""Snapshot ids, and which job a snapshot belongs to.

An id is the start of its run in UTC, ``%Y-%m-%d_%H-%M-%S``. In a config with
several jobs it also names the job - ``2026-07-05_03-15-00_files-nightly`` -
because jobs share the data dir (and may share an S3 bucket + prefix), and two
of them starting in the same second would otherwise write the same files. A
single-job config keeps the plain timestamp, the only form up to 1.7.7, so its
ids do not change. Both forms sort by time, and every command takes either.

Ownership decides which snapshots a job's retention and ``prune`` may delete
and which manifests the healthcheck counts for a job. In a single-job config
the job owns every snapshot except one whose id names another job. In a config
with several jobs:

* an id that names a job belongs to that job;
* a plain id - written before ids were job-scoped, or while the config had one
  job - belongs to the job its ``.offsite-pending.json`` marker names, else to
  the first job (in config order) that stores snapshots in that place: the
  data dir, or one S3 bucket + prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .config.models import Job

LOCAL = "local"  # the data dir as a place; an S3 destination's place is s3_place()
PENDING_SUFFIX = ".offsite-pending.json"  # <id>.offsite-pending.json = {"job": ...}

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


def s3_place(endpoint: Any, bucket: Any, prefix: Any) -> str:
    """The place of an S3 destination: endpoint, bucket and key prefix."""
    return f"s3:{endpoint or ''}|{bucket}|{prefix or ''}"


def job_places(job: Job) -> list[str]:
    """Where ``job`` stores its snapshots: its S3 places, and the data dir when
    it has a local destination or no S3 destination with a bucket (the runner
    then falls back to the data dir)."""
    places = []
    for spec in job.destinations:
        extra = spec.model_extra or {}
        if spec.type == "s3" and extra.get("bucket"):
            places.append(s3_place(extra.get("endpoint"), extra["bucket"], extra.get("prefix")))
    if not places or any(spec.type == "local" for spec in job.destinations):
        places.append(LOCAL)
    return places


def pending_owners(data_dir: Path) -> dict[str, str]:
    """Snapshot id -> the job its off-site pending marker names."""
    owners = {}
    for marker in sorted(Path(data_dir).glob(f"*{PENDING_SUFFIX}")):
        try:
            owner = json.loads(marker.read_text(encoding="utf-8")).get("job")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(owner, str):
            owners[marker.name[: -len(PENDING_SUFFIX)]] = owner
    return owners


@dataclass(frozen=True)
class SnapshotScope:
    """How one job names its snapshots, and which snapshots it owns."""

    job: str
    scoped: bool = False  # ids carry the job name (a config with several jobs)
    # Places where a plain id belongs to this job; None: everywhere, whatever
    # a pending marker says (a single-job config).
    plain_at: Optional[frozenset[str]] = None

    def new_id(self, now: datetime) -> str:
        return new_snapshot_id(now, self.job if self.scoped else None)

    def owns(self, sid: str, place: str = LOCAL, marked_for: Optional[str] = None) -> bool:
        """Whether snapshot ``sid`` at ``place`` belongs to this job;
        ``marked_for`` is the job its pending marker names (data dir only)."""
        _, slug = parse_snapshot_id(sid)
        if slug is not None:
            return slug == job_slug(self.job)
        if self.plain_at is None:
            return True
        if marked_for is not None:
            return marked_for == self.job
        return place in self.plain_at


def scope_for(jobs: Sequence[Job], job: Job) -> SnapshotScope:
    """The scope of ``job`` within the configured ``jobs``."""
    if len(jobs) <= 1:
        return SnapshotScope(job.name)
    first: dict[str, str] = {}
    for other in jobs:
        for place in job_places(other):
            first.setdefault(place, other.name)
    return SnapshotScope(job.name, scoped=True,
                         plain_at=frozenset(p for p, name in first.items() if name == job.name))
