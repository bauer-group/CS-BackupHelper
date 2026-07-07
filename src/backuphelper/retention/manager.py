"""Retention manager — composes count/age/gfs/smart into one prune decision.

An id is pruned iff it is selected by count OR age, AND it is not kept by the
GFS tiers, AND it is not the smart-protected last backup. GFS keeps and smart
protection are safety overrides that always win over the prune selectors.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from backuphelper.config.models import RetentionConfig
from backuphelper.retention import Snapshot, age, count, gfs, smart


def select_prunable(
    snapshots: Iterable[Snapshot], cfg: RetentionConfig, now: datetime
) -> set[str]:
    """Return the set of snapshot ids to prune under the full retention config."""
    snaps = list(snapshots)

    count_prunable = count.select_prunable(snaps, cfg.count)
    age_prunable = age.select_prunable(snaps, cfg.age_days, now)
    gfs_keep = gfs.select_keep(
        snaps, cfg.gfs.daily, cfg.gfs.weekly, cfg.gfs.monthly
    )
    smart_protected = smart.protected_last(snaps) if cfg.smart_last else set()

    candidates = count_prunable | age_prunable
    return candidates - gfs_keep - smart_protected
