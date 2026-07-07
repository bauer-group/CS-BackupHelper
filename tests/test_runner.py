"""Tests for the backup runner (end-to-end orchestration of one job)."""

import json
import shutil
import tarfile
from datetime import datetime, timezone

from backuphelper.archive.manifest import read_manifest, sidecar_path
from backuphelper.config.models import Job, SourceSpec
from backuphelper.runner import restore_snapshot, run_job

NOW = datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc)


class _Spy:
    def __init__(self):
        self.events = []

    def notify(self, event):
        self.events.append(event)


def _fs_job(tmp_path, **over):
    src = tmp_path / "uploads"
    src.mkdir()
    (src / "a.txt").write_text("A")
    spec = {"name": "main",
            "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)},
                        {"type": "env", "name": "env", "whitelist": []}],
            "destinations": [{"type": "local"}]}
    spec.update(over)
    return Job.model_validate(spec)


def test_successful_run_writes_archive_and_sidecar_manifest(tmp_path):
    data = tmp_path / "data"
    spy = _Spy()
    result = run_job(_fs_job(tmp_path), data_dir=data, instance_name="iam",
                     notifier=spy, now=NOW, snapshot_id="2026-07-06_03-00-00")
    assert result.status == "success"
    archive = data / "2026-07-06_03-00-00.tar.gz"
    sidecar = sidecar_path(data, "2026-07-06_03-00-00")
    assert archive.exists() and sidecar.exists()
    assert spy.events and spy.events[0].status == "success"


def test_manifest_has_component_hashes_and_archive_sha256(tmp_path):
    data = tmp_path / "data"
    run_job(_fs_job(tmp_path), data_dir=data, instance_name="iam", now=NOW,
            snapshot_id="s1")
    m = read_manifest(sidecar_path(data, "s1"))
    assert m.archive_sha256 and len(m.archive_sha256) == 64
    kinds = {c.kind for c in m.components}
    assert {"filesystem", "env"} <= kinds
    for c in m.components:
        assert len(c.sha256) == 64


def test_embedded_manifest_is_inside_the_archive(tmp_path):
    data = tmp_path / "data"
    run_job(_fs_job(tmp_path), data_dir=data, instance_name="iam", now=NOW, snapshot_id="s2")
    with tarfile.open(data / "s2.tar.gz", "r:gz") as tar:
        assert "manifest.json" in tar.getnames()


def test_a_failing_source_yields_partial_warning(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(
        SourceSpec(type="filesystem", name="missing", path=str(tmp_path / "does-not-exist"))
    )
    spy = _Spy()
    result = run_job(job, data_dir=data, instance_name="iam", notifier=spy, now=NOW, snapshot_id="s3")
    assert result.status == "warning"
    assert any("missing" in e for e in result.errors)
    assert spy.events[0].status == "warning"


def test_keep_local_false_drops_local_copy_after_s3_upload(tmp_path):
    import boto3
    from moto import mock_aws

    src = tmp_path / "uploads"
    src.mkdir()
    (src / "a.txt").write_text("A")
    data = tmp_path / "data"

    with mock_aws():
        boto3.client("s3", region_name="eu-central-1", aws_access_key_id="k",
                     aws_secret_access_key="s").create_bucket(
            Bucket="offsite", CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        job = Job.model_validate({
            "name": "main", "keep_local": False,
            "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)}],
            "destinations": [{"type": "local"},
                             {"type": "s3", "bucket": "offsite", "access_key": "k",
                              "secret_key": "s", "region": "eu-central-1", "ensure_bucket": False}],
        })
        result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="k1")

    assert result.status == "success"
    assert not (data / "k1.tar.gz").exists()  # local copy dropped
    assert list(data.glob("*.tar.gz")) == []


def test_unconfigured_s3_destination_is_skipped_local_only(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[{"type": "local"}, {"type": "s3", "bucket": ""}])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="s8")
    assert result.status == "success"
    assert (data / "s8.tar.gz").exists()  # no crash on the empty S3 target


def test_only_unconfigured_s3_falls_back_to_local(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[{"type": "s3", "bucket": ""}])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="s7")
    assert result.status == "success"
    assert (data / "s7.tar.gz").exists()  # fell back to local, backup not lost


def test_run_leaves_no_work_artifacts_in_data_dir(tmp_path):
    data = tmp_path / "data"
    run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", now=NOW, snapshot_id="s9")
    top = sorted(p.name for p in data.iterdir())
    assert top == ["s9.manifest.json", "s9.tar.gz"]  # no leftover .work dir


def test_restore_roundtrip_filesystem(tmp_path):
    src = tmp_path / "uploads"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A")
    (src / "sub" / "b.txt").write_text("B")
    data = tmp_path / "data"
    job = Job.model_validate({
        "name": "main",
        "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)}],
        "destinations": [{"type": "local"}],
    })
    run_job(job, data_dir=data, instance_name="iam", now=NOW, snapshot_id="r1")

    shutil.rmtree(src)  # simulate data loss
    assert not src.exists()

    assert restore_snapshot(job, data_dir=data, snapshot_id="r1") is True
    assert (src / "a.txt").read_text() == "A"
    assert (src / "sub" / "b.txt").read_text() == "B"


def test_restore_missing_snapshot_returns_false(tmp_path):
    job = _fs_job(tmp_path)
    assert restore_snapshot(job, data_dir=tmp_path / "data", snapshot_id="nope") is False


def test_retention_prunes_old_local_snapshots(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path, retention={"count": 2})
    for i in range(1, 5):
        run_job(job, data_dir=data, instance_name="iam", now=NOW, snapshot_id=f"2026-07-0{i}_03-00-00")
    remaining = sorted(p.name for p in data.glob("*.tar.gz"))
    assert remaining == ["2026-07-03_03-00-00.tar.gz", "2026-07-04_03-00-00.tar.gz"]
