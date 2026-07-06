"""Tests for the retention manager (composes count/age/gfs/smart via config).

Snapshot ids are ISO-timestamp strings kept consistent with ``when`` so the
"newest = lexicographically greatest id" contract holds throughout.
"""

from datetime import datetime

from backuphelper.config.models import GFSConfig, RetentionConfig
from backuphelper.retention import Snapshot
from backuphelper.retention import manager

NOW = datetime(2026, 2, 1, 12, 0, 0)


def _snap(id_: str, when: datetime) -> Snapshot:
    return Snapshot(id=id_, when=when)


def test_count_drives_pruning_with_gfs_and_age_disabled():
    snaps = [
        _snap("2026-01-01", datetime(2026, 1, 1)),
        _snap("2026-01-02", datetime(2026, 1, 2)),
        _snap("2026-01-03", datetime(2026, 1, 3)),
        _snap("2026-01-04", datetime(2026, 1, 4)),
    ]
    cfg = RetentionConfig(count=2, age_days=0, gfs=GFSConfig(), smart_last=True)
    assert manager.select_prunable(snaps, cfg, NOW) == {"2026-01-01", "2026-01-02"}


def test_age_prunes_old_even_when_count_would_keep_them():
    # count=10 keeps everything -> the prune decision comes from age alone.
    snaps = [
        _snap("2026-01-01", datetime(2026, 1, 1)),
        _snap("2026-01-31", datetime(2026, 1, 31)),
    ]
    cfg = RetentionConfig(count=10, age_days=7, gfs=GFSConfig(), smart_last=True)
    assert manager.select_prunable(snaps, cfg, NOW) == {"2026-01-01"}


def test_smart_protects_newest_from_age_pruning():
    snaps = [
        _snap("2026-01-01", datetime(2026, 1, 1)),
        _snap("2026-01-02", datetime(2026, 1, 2)),
        _snap("2026-01-03", datetime(2026, 1, 3)),
    ]
    cfg = RetentionConfig(count=0, age_days=7, gfs=GFSConfig(), smart_last=True)
    # Everything is past the cutoff, but the newest (01-03) is protected.
    assert manager.select_prunable(snaps, cfg, NOW) == {"2026-01-01", "2026-01-02"}


def test_smart_last_false_lets_newest_be_pruned():
    snaps = [
        _snap("2026-01-01", datetime(2026, 1, 1)),
        _snap("2026-01-02", datetime(2026, 1, 2)),
        _snap("2026-01-03", datetime(2026, 1, 3)),
    ]
    cfg = RetentionConfig(count=0, age_days=7, gfs=GFSConfig(), smart_last=False)
    assert manager.select_prunable(snaps, cfg, NOW) == {
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    }


def test_gfs_keep_overrides_count_pruning():
    snaps = [
        _snap("2026-01-10", datetime(2026, 1, 10)),
        _snap("2026-01-20", datetime(2026, 1, 20)),
        _snap("2026-02-05", datetime(2026, 2, 5)),
    ]
    # count=1 would prune both January snapshots, but monthly GFS keeps the
    # newest of January (01-20), so only 01-10 is actually pruned.
    cfg = RetentionConfig(
        count=1,
        age_days=0,
        gfs=GFSConfig(daily=0, weekly=0, monthly=2),
        smart_last=True,
    )
    assert manager.select_prunable(snaps, cfg, datetime(2026, 3, 1)) == {"2026-01-10"}


def test_count_zero_safety_keeps_everything():
    snaps = [
        _snap("2026-01-01", datetime(2026, 1, 1)),
        _snap("2026-01-02", datetime(2026, 1, 2)),
    ]
    cfg = RetentionConfig(count=0, age_days=0, gfs=GFSConfig(), smart_last=True)
    assert manager.select_prunable(snaps, cfg, NOW) == set()


def test_empty_input_prunes_nothing():
    cfg = RetentionConfig(count=2, age_days=7, gfs=GFSConfig(), smart_last=True)
    assert manager.select_prunable([], cfg, NOW) == set()


def test_returns_a_set_of_ids():
    snaps = [
        _snap("2026-01-01", datetime(2026, 1, 1)),
        _snap("2026-01-02", datetime(2026, 1, 2)),
        _snap("2026-01-03", datetime(2026, 1, 3)),
    ]
    cfg = RetentionConfig(count=1, age_days=0, gfs=GFSConfig(), smart_last=True)
    result = manager.select_prunable(snaps, cfg, NOW)
    assert isinstance(result, set)
    assert sorted(result) == ["2026-01-01", "2026-01-02"]
