"""Retention policies — pure functions selecting snapshot ids to prune/keep.

A ``Snapshot`` is the minimal unit every policy reasons over: an ``id``
(a sortable timestamp string, newest = lexicographically greatest) and the
``when`` it was taken. Every policy here is a pure function (no I/O) that
returns a ``set[str]`` of snapshot ids; ``manager`` composes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Snapshot:
    """A backup snapshot: sortable ``id`` plus the time it was taken."""

    id: str
    when: datetime
