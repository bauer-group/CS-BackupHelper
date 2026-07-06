"""Age-based retention: prune snapshots older than ``now - max_age_days``."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from backuphelper.retention import Snapshot


def select_prunable(
    snapshots: Iterable[Snapshot], max_age_days: int, now: datetime
) -> set[str]:
    """Return ids whose ``when`` is older than the cutoff ``now - max_age_days``.

    ``max_age_days <= 0`` disables age-based pruning (prune nothing).
    """
    if max_age_days <= 0:
        return set()
    cutoff = now - timedelta(days=max_age_days)
    return {s.id for s in snapshots if s.when < cutoff}
