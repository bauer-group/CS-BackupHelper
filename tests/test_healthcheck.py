"""Tests for the functional healthcheck (last-backup staleness)."""

from datetime import datetime, timedelta, timezone

from backuphelper.archive.manifest import Manifest, sidecar_path, write_manifest
from backuphelper.healthcheck import is_healthy

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def _write(dir_, snapshot_id, created_at):
    m = Manifest.build(snapshot_id=snapshot_id, instance_name="i", components=[],
                       created_at=created_at)
    write_manifest(m, sidecar_path(dir_, snapshot_id))


def test_no_manifest_is_healthy_grace(tmp_path):
    # A freshly started daemon that has not run yet must not be marked unhealthy.
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_fresh_manifest_is_healthy(tmp_path):
    _write(tmp_path, "s1", (NOW - timedelta(hours=2)).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True


def test_stale_manifest_is_unhealthy(tmp_path):
    _write(tmp_path, "s1", (NOW - timedelta(hours=48)).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is False


def test_uses_the_newest_manifest(tmp_path):
    _write(tmp_path, "old", (NOW - timedelta(hours=48)).isoformat())
    _write(tmp_path, "new", (NOW - timedelta(hours=1)).isoformat())
    assert is_healthy(tmp_path, max_age_hours=26, now=NOW) is True
