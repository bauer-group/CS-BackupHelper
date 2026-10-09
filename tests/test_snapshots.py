"""Tests for snapshot ids (plain and job-scoped)."""

from datetime import datetime, timezone

from backuphelper.config.models import Job
from backuphelper.snapshots import (
    SnapshotScope,
    job_slug,
    new_snapshot_id,
    parse_snapshot_id,
    scope_for,
)

NOW = datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc)


def test_a_plain_id_is_the_timestamp_as_before():
    assert new_snapshot_id(NOW) == "2026-07-06_03-00-00"
    assert parse_snapshot_id("2026-07-06_03-00-00") == (NOW, None)


def test_a_job_scoped_id_carries_the_job_and_its_timestamp():
    sid = new_snapshot_id(NOW, "files-nightly")
    assert sid == "2026-07-06_03-00-00_files-nightly"
    assert parse_snapshot_id(sid) == (NOW, "files-nightly")


def test_a_job_name_is_reduced_to_safe_characters():
    # No "." (a snapshot's files are found by the prefix "<id>."), no "/".
    assert job_slug("db.hourly") == "db-hourly"
    assert job_slug("../etc x") == "---etc-x"
    assert job_slug("files_2") == "files_2"
    assert new_snapshot_id(NOW, "db.hourly") == "2026-07-06_03-00-00_db-hourly"


def test_ids_the_engine_did_not_generate_have_no_time_or_job():
    for sid in ("s1", "k1", "2026-07-06", "2026-07-06_03-00-00x", "2026-07-06_03-00-00_",
                "2026-07-06_03-00-00_a.b"):
        assert parse_snapshot_id(sid) == (None, None), sid


def test_only_a_config_with_several_jobs_scopes_its_ids():
    one, two = Job(name="main"), Job(name="files")
    assert scope_for([one], one) == SnapshotScope("main", scoped=False)
    assert scope_for([one, two], two) == SnapshotScope("files", scoped=True)
    assert scope_for([one], one).new_id(NOW) == "2026-07-06_03-00-00"
    assert scope_for([one, two], one).new_id(NOW) == "2026-07-06_03-00-00_main"


def test_scoped_ids_sort_by_time_among_plain_ones():
    ids = [new_snapshot_id(NOW.replace(hour=4), "b"), new_snapshot_id(NOW.replace(hour=2)),
           new_snapshot_id(NOW, "a")]
    assert sorted(ids) == ["2026-07-06_02-00-00", "2026-07-06_03-00-00_a",
                           "2026-07-06_04-00-00_b"]


def test_the_retention_timestamp_of_a_scoped_id_is_its_real_time():
    # Regression guard: a job-scoped id must not fall back to "now", or age
    # and GFS retention would treat every scoped snapshot as brand new.
    from backuphelper.runner import parse_snapshot_timestamp

    fallback = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert parse_snapshot_timestamp("2026-07-06_03-00-00_db", fallback) == NOW
    assert parse_snapshot_timestamp("custom", fallback) == fallback
