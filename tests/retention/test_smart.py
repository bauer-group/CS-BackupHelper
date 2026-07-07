"""Tests for smart protection (never prune the sole/last backup)."""

from datetime import datetime

from backuphelper.retention import Snapshot
from backuphelper.retention import smart


def _snap(id_: str) -> Snapshot:
    return Snapshot(id=id_, when=datetime(2026, 1, 1))


def test_protects_single_newest_snapshot():
    snaps = [_snap("2026-01-01"), _snap("2026-01-03"), _snap("2026-01-02")]
    assert smart.protected_last(snaps) == {"2026-01-03"}


def test_empty_input_protects_nothing():
    assert smart.protected_last([]) == set()


def test_single_snapshot_is_protected():
    assert smart.protected_last([_snap("only")]) == {"only"}
