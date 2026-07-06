"""Grandfather-father-son retention: ids to KEEP across day/week/month tiers.

Each tier keeps the newest snapshot of the newest ``N`` distinct buckets
(calendar days / ISO weeks / year-months). ``N == 0`` disables that tier.
The kept sets union across tiers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from backuphelper.retention import Snapshot

# A bucket key must be orderable so "newest" buckets sort last.
BucketKey = tuple[int, ...]


def _keep_for_tier(
    snapshots: list[Snapshot], count: int, bucket: Callable[[Snapshot], BucketKey]
) -> set[str]:
    """Keep the newest snapshot (by id) of the newest ``count`` distinct buckets."""
    if count <= 0:
        return set()
    newest_in_bucket: dict[BucketKey, Snapshot] = {}
    for snap in snapshots:
        key = bucket(snap)
        current = newest_in_bucket.get(key)
        if current is None or snap.id > current.id:
            newest_in_bucket[key] = snap
    kept_buckets = sorted(newest_in_bucket, reverse=True)[:count]
    return {newest_in_bucket[key].id for key in kept_buckets}


def select_keep(
    snapshots: Iterable[Snapshot], daily: int, weekly: int, monthly: int
) -> set[str]:
    """Return the union of ids kept by the daily, weekly and monthly tiers."""
    snaps = list(snapshots)
    keep: set[str] = set()
    keep |= _keep_for_tier(
        snaps, daily, lambda s: (s.when.year, s.when.month, s.when.day)
    )
    keep |= _keep_for_tier(
        snaps, weekly, lambda s: s.when.isocalendar()[:2]
    )
    keep |= _keep_for_tier(snaps, monthly, lambda s: (s.when.year, s.when.month))
    return keep
