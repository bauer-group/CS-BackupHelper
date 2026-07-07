"""Smart protection: never prune the sole/last backup of a source."""

from __future__ import annotations

from collections.abc import Iterable

from backuphelper.retention import Snapshot


def protected_last(snapshots: Iterable[Snapshot]) -> set[str]:
    """Return the id of the single newest snapshot, or an empty set if none."""
    ids = [s.id for s in snapshots]
    if not ids:
        return set()
    return {max(ids)}
