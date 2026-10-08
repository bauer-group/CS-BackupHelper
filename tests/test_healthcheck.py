"""Tests for the functional healthcheck (docs/deployment.md#the-functional-healthcheck).

The state helpers are imported inside the tests that need them, so every rule
is checked on its own against an engine that does not have them yet.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from backuphelper.archive.manifest import Component, Manifest, sidecar_path, write_manifest
from backuphelper.healthcheck import is_healthy

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ago(hours):
    return NOW - timedelta(hours=hours)


def _write(dir_, snapshot_id, created_at, failed=()):
    components = [Component(name="uploads", kind="filesystem", size=1, sha256="a")]
    components += [Component(name=n, kind="postgres", size=0, sha256="", error="pg_dump failed")
                   for n in failed]
    m = Manifest.build(snapshot_id=snapshot_id, instance_name="i", components=components,
                       created_at=created_at)
    write_manifest(m, sidecar_path(dir_, snapshot_id))


def _write_pre_177(dir_, snapshot_id, created_at, error=None):
    """A sidecar as 1.7.6 wrote it: no status field."""
    sidecar_path(dir_, snapshot_id).write_text(json.dumps({
        "schema_version": 1, "snapshot_id": snapshot_id, "instance_name": "i",
        "created_at": created_at, "total_bytes": 1, "archive_sha256": "x",
        "components": [{"name": "uploads", "kind": "filesystem", "size": 1, "sha256": "a",
                        "error": None, "metadata": {}},
                       {"name": "database", "kind": "postgres", "size": 0, "sha256": "",
                        "error": error, "metadata": {}}]}))


def _daemon_started(dir_, hours_ago):
    from backuphelper.state import record_daemon_start

    record_daemon_start(dir_, now=_ago(hours_ago))


def _ran(dir_, hours_ago, status, job="main", failed=()):
    from backuphelper.state import RunRecord, record_run

    record_run(dir_, RunRecord(job=job, snapshot_id=f"{job}-{hours_ago}", status=status,
                               started_at=_ago(hours_ago), failed_components=tuple(failed)))


def _check(dir_, max_age_hours=26):
    from backuphelper.healthcheck import check

    return check(dir_, max_age_hours, now=NOW)


# ── grace for a daemon that has not run a backup yet ─────────────────────────
def test_no_backup_yet_is_healthy_within_the_grace_after_the_daemon_start(tmp_path):
    # A freshly started stack must become healthy (docker compose up --wait).
    _daemon_started(tmp_path, hours_ago=0)
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True
    _daemon_started(tmp_path, hours_ago=25.9)
    health = _check(tmp_path)
    assert health.healthy and "grace" in health.reason


def test_no_backup_after_the_grace_is_unhealthy(tmp_path):
    # Regression: "no manifest" was healthy forever, so a daemon whose every
    # run failed before writing a snapshot never turned red.
    _daemon_started(tmp_path, hours_ago=27)
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False
    assert "no backup has run" in _check(tmp_path).reason


def test_no_backup_and_no_recorded_daemon_start_is_unhealthy(tmp_path):
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


def test_the_grace_follows_the_configured_max_age(tmp_path):
    # A weekly schedule needs a max age above one week - and gets that grace.
    _daemon_started(tmp_path, hours_ago=100)
    assert is_healthy(tmp_path, max_age_hours=170, now=NOW) is True
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


# ── freshness ────────────────────────────────────────────────────────────────
def test_fresh_manifest_is_healthy(tmp_path):
    _write(tmp_path, "s1", _ago(2).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_stale_manifest_is_unhealthy(tmp_path):
    _write(tmp_path, "s1", _ago(48).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


def test_uses_the_newest_manifest(tmp_path):
    _write(tmp_path, "old", _ago(48).isoformat())
    _write(tmp_path, "new", _ago(1).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_a_stale_backup_is_not_rescued_by_a_recent_daemon_start(tmp_path):
    _write(tmp_path, "s1", _ago(48).isoformat())
    _daemon_started(tmp_path, hours_ago=0)
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


# ── failed runs ──────────────────────────────────────────────────────────────
def test_newest_snapshot_with_a_failed_component_is_unhealthy(tmp_path):
    # Regression: a fresh snapshot without its database counted as healthy.
    _write(tmp_path, "s1", _ago(1).isoformat(), failed=["database"])
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False
    assert "database" in _check(tmp_path).reason


def test_a_failed_component_in_a_manifest_written_before_1_7_7_counts(tmp_path):
    _write_pre_177(tmp_path, "s1", _ago(1).isoformat(), error="pg_dump failed: timeout")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False
    _write_pre_177(tmp_path, "s2", _ago(0.5).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_manifest_status_error_is_unhealthy(tmp_path):
    _write(tmp_path, "s1", _ago(1).isoformat())
    path = sidecar_path(tmp_path, "s1")
    path.write_text(json.dumps({**json.loads(path.read_text()), "status": "error"}))
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


def test_a_newer_good_snapshot_clears_the_failure(tmp_path):
    _write(tmp_path, "bad", _ago(5).isoformat(), failed=["database"])
    _write(tmp_path, "good", _ago(1).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_a_run_that_ended_in_error_is_unhealthy(tmp_path):
    # e.g. a run that aborted (pre_backup hook raised) and wrote no manifest
    _write(tmp_path, "good", _ago(5).isoformat())
    _ran(tmp_path, 1, "error")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False
    _ran(tmp_path, 0.5, "success")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_a_warning_run_is_healthy(tmp_path):
    # warnings (an unreachable S3 destination, skipped files) are not failures
    _ran(tmp_path, 1, "warning")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_on_equal_timestamps_the_run_record_decides(tmp_path):
    # The record knows the job outcome; the manifest only the snapshot content.
    _write(tmp_path, "main-1", _ago(1).isoformat())
    _ran(tmp_path, 1, "error")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


def test_the_most_recent_run_across_jobs_decides(tmp_path):
    _ran(tmp_path, 5, "error", job="files-offsite", failed=["uploads"])
    _ran(tmp_path, 1, "success", job="database-hourly")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True
    _ran(tmp_path, 0.5, "error", job="files-offsite", failed=["uploads"])
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


# ── keep_local=false: no local manifest, the run record keeps the truth ──────
def test_run_record_detects_staleness_without_any_manifest(tmp_path):
    # Regression: with keep_local=false no manifest stays local, so the probe
    # sat in its grace forever and never reported a stopped backup.
    _daemon_started(tmp_path, hours_ago=200)
    _ran(tmp_path, 30, "success")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False
    assert "stale" in _check(tmp_path).reason
    _ran(tmp_path, 2, "success")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_a_corrupt_run_record_is_ignored(tmp_path):
    _write(tmp_path, "s1", _ago(1).isoformat())
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "job-main.json").write_text("{not json")
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


# ── the data dir must take new backups ───────────────────────────────────────
def test_unwritable_data_dir_is_unhealthy(tmp_path, monkeypatch):
    # Regression: a volume the backup user cannot write was reported healthy
    # while every run failed. (os.access is faked: CI may run as root.)
    _write(tmp_path, "s1", _ago(1).isoformat())
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, mode: False if str(p) == str(tmp_path)
                        else real_access(p, mode))
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False
    assert "not writable" in _check(tmp_path).reason


def test_unwritable_state_dir_is_unhealthy(tmp_path, monkeypatch):
    _ran(tmp_path, 1, "success")
    state = tmp_path / ".state"
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, mode: False if str(p) == str(state)
                        else real_access(p, mode))
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


def test_missing_data_dir_is_unhealthy(tmp_path):
    assert is_healthy(tmp_path / "gone", max_age_hours=26, now=NOW) is False


def test_the_check_writes_nothing(tmp_path):
    _daemon_started(tmp_path, hours_ago=1)
    _ran(tmp_path, 1, "error", failed=["database"])
    _write(tmp_path, "s1", _ago(2).isoformat())
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    for max_age in (0.1, 26):
        _check(tmp_path, max_age)
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
