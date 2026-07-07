"""Tests for age-based retention (prune snapshots older than a cutoff)."""

from datetime import datetime, timedelta

from backuphelper.retention import Snapshot
from backuphelper.retention import age

NOW = datetime(2026, 1, 31, 12, 0, 0)


def _snap(id_: str, when: datetime) -> Snapshot:
    return Snapshot(id=id_, when=when)


def test_prunes_snapshots_older_than_cutoff():
    old = _snap("old", datetime(2026, 1, 1))
    fresh = _snap("fresh", datetime(2026, 1, 30))
    assert age.select_prunable([old, fresh], max_age_days=7, now=NOW) == {"old"}


def test_snapshot_exactly_at_cutoff_is_kept():
    # cutoff is strictly-less-than: an item AT the boundary is not "older".
    at_cutoff = _snap("edge", NOW - timedelta(days=7))
    assert age.select_prunable([at_cutoff], max_age_days=7, now=NOW) == set()


def test_snapshot_one_second_past_cutoff_is_pruned():
    past = _snap("past", NOW - timedelta(days=7, seconds=1))
    assert age.select_prunable([past], max_age_days=7, now=NOW) == {"past"}


def test_max_age_zero_disables_pruning():
    old = _snap("old", datetime(2000, 1, 1))
    assert age.select_prunable([old], max_age_days=0, now=NOW) == set()


def test_negative_max_age_disables_pruning():
    old = _snap("old", datetime(2000, 1, 1))
    assert age.select_prunable([old], max_age_days=-3, now=NOW) == set()


def test_empty_input_prunes_nothing():
    assert age.select_prunable([], max_age_days=7, now=NOW) == set()
