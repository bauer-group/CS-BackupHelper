"""Count-based retention: keep the newest ``keep`` snapshots, prune the rest."""

from __future__ import annotations

from collections.abc import Iterable

from backuphelper.retention import Snapshot


def select_prunable(snapshots: Iterable[Snapshot], keep: int) -> set[str]:
    """Return ids to prune, keeping the newest ``keep`` by id.

    ``keep <= 0`` is a safety rule: keep EVERYTHING (prune nothing).
    """
    if keep <= 0:
        return set()
    ordered = sorted(snapshots, key=lambda s: s.id, reverse=True)
    return {s.id for s in ordered[keep:]}
