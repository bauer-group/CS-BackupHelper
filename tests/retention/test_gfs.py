"""Tests for grandfather-father-son retention (returns ids to KEEP).

All datetimes are fixed literals — no ``datetime.now()`` — so day/week/month
boundary behaviour is deterministic.
"""

from datetime import datetime

from backuphelper.retention import Snapshot
from backuphelper.retention import gfs


def _snap(id_: str, when: datetime) -> Snapshot:
    return Snapshot(id=id_, when=when)


def test_daily_keeps_newest_snapshot_of_the_newest_distinct_days():
    snaps = [
        _snap("2026-01-10T01", datetime(2026, 1, 10, 1)),
        _snap("2026-01-10T09", datetime(2026, 1, 10, 9)),
        _snap("2026-01-11T05", datetime(2026, 1, 11, 5)),
        _snap("2026-01-12T05", datetime(2026, 1, 12, 5)),
    ]
    # 2 newest days: 01-12 and 01-11; newest snapshot per kept day.
    assert gfs.select_keep(snaps, daily=2, weekly=0, monthly=0) == {
        "2026-01-12T05",
        "2026-01-11T05",
    }


def test_weekly_keeps_newest_snapshot_of_newest_distinct_iso_weeks():
    snaps = [
        _snap("wk2", datetime(2026, 1, 5)),   # ISO week 2
        _snap("wk3", datetime(2026, 1, 12)),  # ISO week 3
        _snap("wk4", datetime(2026, 1, 19)),  # ISO week 4
    ]
    assert gfs.select_keep(snaps, daily=0, weekly=2, monthly=0) == {"wk4", "wk3"}


def test_weekly_treats_sunday_and_following_monday_as_different_weeks():
    sunday = _snap("sun", datetime(2026, 1, 4))    # ISO week 1 (Sunday)
    monday = _snap("mon", datetime(2026, 1, 5))    # ISO week 2 (Monday)
    # Only the newest week is kept -> the Monday snapshot.
    assert gfs.select_keep([sunday, monday], daily=0, weekly=1, monthly=0) == {"mon"}


def test_monthly_keeps_newest_snapshot_of_newest_distinct_months():
    snaps = [
        _snap("jan", datetime(2026, 1, 15)),
        _snap("feb", datetime(2026, 2, 15)),
        _snap("mar", datetime(2026, 3, 15)),
    ]
    assert gfs.select_keep(snaps, daily=0, weekly=0, monthly=2) == {"mar", "feb"}


def test_monthly_treats_month_end_and_next_month_start_as_different_months():
    jan_end = _snap("jan31", datetime(2026, 1, 31, 23))
    feb_start = _snap("feb01", datetime(2026, 2, 1, 1))
    assert gfs.select_keep(
        [jan_end, feb_start], daily=0, weekly=0, monthly=1
    ) == {"feb01"}


def test_all_zero_tiers_keep_nothing():
    snaps = [_snap("a", datetime(2026, 1, 1)), _snap("b", datetime(2026, 1, 2))]
    assert gfs.select_keep(snaps, daily=0, weekly=0, monthly=0) == set()


def test_empty_input_keeps_nothing():
    assert gfs.select_keep([], daily=5, weekly=5, monthly=5) == set()


def test_tiers_union_and_drop_snapshots_no_tier_covers():
    snaps = [
        _snap("A", datetime(2026, 1, 5)),
        _snap("B", datetime(2026, 1, 31)),
        _snap("C", datetime(2026, 2, 10)),
        _snap("D", datetime(2026, 2, 20)),
    ]
    # daily=2 -> {D, C}; weekly=1 -> {D}; monthly=2 -> {D (Feb), B (Jan)}.
    # Union = {B, C, D}; A is covered by no kept bucket.
    assert gfs.select_keep(snaps, daily=2, weekly=1, monthly=2) == {"B", "C", "D"}
