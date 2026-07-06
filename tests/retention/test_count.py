"""Tests for count-based retention (keep newest N, prune the rest)."""

from datetime import datetime

from backuphelper.retention import Snapshot
from backuphelper.retention import count


def _snap(id_: str) -> Snapshot:
    return Snapshot(id=id_, when=datetime(2026, 1, 1))


def test_keeps_newest_and_prunes_older():
    snaps = [_snap("2026-01-01"), _snap("2026-01-02"), _snap("2026-01-03")]
    assert count.select_prunable(snaps, keep=2) == {"2026-01-01"}


def test_order_of_input_does_not_matter():
    snaps = [_snap("2026-01-03"), _snap("2026-01-01"), _snap("2026-01-02")]
    assert count.select_prunable(snaps, keep=1) == {"2026-01-01", "2026-01-02"}


def test_keep_zero_keeps_everything_safety_rule():
    snaps = [_snap("2026-01-01"), _snap("2026-01-02")]
    assert count.select_prunable(snaps, keep=0) == set()


def test_negative_keep_keeps_everything_safety_rule():
    snaps = [_snap("2026-01-01"), _snap("2026-01-02")]
    assert count.select_prunable(snaps, keep=-5) == set()


def test_keep_greater_than_count_prunes_nothing():
    snaps = [_snap("2026-01-01"), _snap("2026-01-02")]
    assert count.select_prunable(snaps, keep=10) == set()


def test_empty_input_prunes_nothing():
    assert count.select_prunable([], keep=3) == set()
