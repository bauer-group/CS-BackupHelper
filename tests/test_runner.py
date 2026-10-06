"""Tests for the backup runner (end-to-end orchestration of one job)."""

import json
import shutil
import tarfile
from datetime import datetime, timedelta, timezone

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


def test_failed_encryption_warns_that_the_snapshot_is_unencrypted(tmp_path, monkeypatch, caplog):
    import backuphelper.runner as runner
    from backuphelper.encryption.engine import EncryptionError

    def boom(*_a, **_k):
        raise EncryptionError("age binary not found")

    monkeypatch.setattr(runner, "encrypt", boom)
    data = tmp_path / "data"
    spy = _Spy()
    job = _fs_job(tmp_path, encryption={"mode": "age", "recipient": "age1abc"})
    with caplog.at_level("ERROR", logger="backuphelper.runner"):
        result = run_job(job, data_dir=data, instance_name="iam", notifier=spy, now=NOW,
                         snapshot_id="enc1")
    # availability over confidentiality: the snapshot exists, but unencrypted ...
    assert (data / "enc1.tar.gz").exists()
    assert not list(data.glob("enc1.tar.gz.*"))
    # ... and every channel says so explicitly
    assert result.status == "warning"
    assert any("UNENCRYPTED" in e and "age binary not found" in e for e in result.errors)
    assert spy.events[0].status == "warning"
    assert any("UNENCRYPTED" in e for e in spy.events[0].errors)
    assert any(r.levelname == "ERROR" and "UNENCRYPTED" in r.getMessage() for r in caplog.records)


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


def test_a_raising_source_is_recorded_in_the_manifest(tmp_path, monkeypatch):
    # Regression: a source whose produce() RAISED (e.g. PermissionError) vanished
    # from the manifest — no entry, no error — while a failed pg_dump (returned as
    # an errored component) was recorded with size 0 and its error text.
    from backuphelper.sources.filesystem import FilesystemSource

    real_produce = FilesystemSource.produce

    def produce(self, staging_dir):
        if self.cfg.name != "locked":
            return real_produce(self, staging_dir)
        (staging_dir / "locked.tar.gz").write_bytes(b"half-written")
        raise PermissionError(13, "Permission denied", "/srv/locked/private.txt")

    monkeypatch.setattr(FilesystemSource, "produce", produce)
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="filesystem", name="locked", path=str(tmp_path)))
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="f1")

    assert result.status == "warning"
    comp = {c.name: c for c in read_manifest(sidecar_path(data, "f1")).components}["locked"]
    assert comp.kind == "filesystem" and comp.size == 0 and comp.sha256 == ""
    assert "Permission denied" in comp.error
    assert any(e.startswith("locked:") and "Permission denied" in e for e in result.errors)
    with tarfile.open(data / "f1.tar.gz", "r:gz") as tar:  # no half-written output shipped
        assert "locked.tar.gz" not in tar.getnames()


def test_a_source_that_cannot_be_built_is_recorded_in_the_manifest(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="nocodb", name="nocodb"))  # plugin not installed
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="f2")
    assert result.status == "warning"
    comp = {c.name: c for c in read_manifest(sidecar_path(data, "f2")).components}["nocodb"]
    assert comp.kind == "nocodb" and comp.size == 0 and comp.sha256 == "" and comp.error


def test_an_unreadable_directory_is_recorded_as_a_failed_component(tmp_path, monkeypatch):
    # End to end: a directory the backup user cannot list used to be archived as
    # an empty, healthy-looking component (sha256 set, no error, status success).
    import os

    real_scandir = os.scandir
    locked = tmp_path / "uploads"

    def scandir(path="."):
        if str(path) == str(locked):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    monkeypatch.setattr(os, "scandir", scandir)
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="f3")
    assert result.status == "warning"
    comp = {c.name: c for c in read_manifest(sidecar_path(data, "f3")).components}["uploads"]
    assert comp.size == 0 and comp.sha256 == ""
    assert comp.error.startswith("PermissionError:") and "uploads" in comp.error


def _s3_dest(**over):
    spec = {"type": "s3", "bucket": "offsite", "access_key": "k", "secret_key": "s",
            "region": "eu-central-1"}
    spec.update(over)
    return spec


class _EmptyS3:
    """boto3 stand-in for a reachable, empty bucket: retention lists no keys
    (without it, list_keys would retry with real backoff sleeps)."""

    def get_paginator(self, _name):
        return self

    def paginate(self, **_kwargs):
        return [{}]


def test_keep_local_false_keeps_the_only_copy_when_s3_is_unconfigured(tmp_path):
    # Regression: an S3 destination with an empty bucket is skipped, yet
    # keep_local=false still deleted the local archive — the ONLY copy — and the
    # run reported success.
    data = tmp_path / "data"
    job = _fs_job(tmp_path, keep_local=False,
                  destinations=[{"type": "local"}, _s3_dest(bucket="")])
    spy = _Spy()
    result = run_job(job, data_dir=data, instance_name="i", notifier=spy, now=NOW,
                     snapshot_id="k2")
    assert (data / "k2.tar.gz").exists() and (data / "k2.manifest.json").exists()
    assert result.archive == data / "k2.tar.gz"
    assert result.status == "warning"
    assert any("keep_local" in e for e in result.errors)
    assert spy.events[0].status == "warning"


def test_keep_local_false_keeps_local_when_the_s3_upload_fails(tmp_path, monkeypatch):
    from backuphelper.destinations.s3 import S3Destination

    def failing_put(self, local_path, key):
        raise RuntimeError("upload size mismatch")

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _EmptyS3())
    monkeypatch.setattr(S3Destination, "put", failing_put)
    data = tmp_path / "data"
    job = _fs_job(tmp_path, keep_local=False,
                  destinations=[{"type": "local"}, _s3_dest(ensure_bucket=False)])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="k3")
    assert (data / "k3.tar.gz").exists()
    assert result.status == "warning"


def test_keep_local_false_keeps_local_when_only_the_remote_manifest_fails(tmp_path, monkeypatch):
    # A remote archive without its sidecar cannot be listed, verified or
    # hydrated, so it does not count as an off-site copy.
    from backuphelper.destinations.s3 import S3Destination

    uploaded = []

    def put(self, local_path, key):
        if key.endswith(".manifest.json"):
            raise RuntimeError("manifest upload rejected")
        uploaded.append(key)

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _EmptyS3())
    monkeypatch.setattr(S3Destination, "put", put)
    data = tmp_path / "data"
    job = _fs_job(tmp_path, keep_local=False,
                  destinations=[{"type": "local"}, _s3_dest(ensure_bucket=False)])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="k4")
    assert uploaded == ["k4.tar.gz"]  # the archive itself did reach the bucket
    assert (data / "k4.tar.gz").exists() and (data / "k4.manifest.json").exists()
    assert result.status == "warning"
    assert any(e.startswith("upload failed:") for e in result.errors)


class _ForbiddenS3:
    """boto3 stand-in for rejected credentials: head_bucket answers 403."""

    def head_bucket(self, **_kwargs):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket")


def test_unreachable_s3_destination_degrades_to_warning_and_keeps_local(tmp_path, monkeypatch):
    # Regression: S3Destination construction (client + ensure_bucket) ran outside
    # any try, so a 403 / DNS error aborted the run — no snapshot stored, no alert,
    # .work left behind.
    from backuphelper.destinations.s3 import S3Destination

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    data = tmp_path / "data"
    spy = _Spy()
    job = _fs_job(tmp_path, keep_local=False, destinations=[{"type": "local"}, _s3_dest()])
    result = run_job(job, data_dir=data, instance_name="i", notifier=spy, now=NOW,
                     snapshot_id="u1")
    assert result.status == "warning"
    assert (data / "u1.tar.gz").exists() and (data / "u1.manifest.json").exists()
    assert any("offsite" in e and "403" in e for e in result.errors)
    assert spy.events[0].status == "warning" and spy.events[0].errors == result.errors
    assert not (data / ".work").exists()


def test_s3_only_job_keeps_the_snapshot_locally_when_s3_is_unreachable(tmp_path, monkeypatch):
    from backuphelper.destinations.s3 import S3Destination

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[_s3_dest()])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="u2")
    assert result.status == "warning"
    assert result.archive == data / "u2.tar.gz" and result.archive.exists()
    assert any("kept it in the local data dir" in e for e in result.errors)


def test_s3_only_job_keeps_the_snapshot_locally_when_the_upload_fails(tmp_path, monkeypatch):
    from backuphelper.destinations.s3 import S3Destination

    def failing_put(self, local_path, key):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _EmptyS3())
    monkeypatch.setattr(S3Destination, "put", failing_put)
    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[_s3_dest(ensure_bucket=False)])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="u3")
    assert result.status == "warning"
    assert (data / "u3.tar.gz").exists() and (data / "u3.manifest.json").exists()
    assert any("upload failed" in e for e in result.errors)
    assert any("kept it in the local data dir" in e for e in result.errors)


def test_work_dir_is_removed_when_the_run_aborts(tmp_path):
    # A pre_backup gate may abort the run by raising; the staging area must not
    # be left behind in the data dir.
    import pytest

    from backuphelper.plugins.hooks import HookRegistry

    def refuse(_ctx):
        raise RuntimeError("app refused to quiesce")

    hooks = HookRegistry()
    hooks.register("pre_backup", refuse)
    data = tmp_path / "data"
    with pytest.raises(RuntimeError):
        run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", now=NOW,
                snapshot_id="u4", hooks=hooks)
    assert not (data / ".work").exists()


def test_an_aborted_run_still_sends_an_error_alert(tmp_path):
    # A raising pre_backup gate (or a full disk while bundling) propagates as
    # before, but must not fail silently: the daemon only logged it, no alert.
    import pytest

    from backuphelper.plugins.hooks import HookRegistry

    def refuse(_ctx):
        raise RuntimeError("app refused to quiesce")

    hooks = HookRegistry()
    hooks.register("pre_backup", refuse)
    spy = _Spy()
    with pytest.raises(RuntimeError, match="quiesce"):
        run_job(_fs_job(tmp_path), data_dir=tmp_path / "data", instance_name="i",
                notifier=spy, now=NOW, snapshot_id="u5", hooks=hooks)
    [event] = spy.events
    assert event.status == "error" and event.snapshot_id == "u5"
    assert event.errors == ["run aborted: RuntimeError: app refused to quiesce"]


class _MemoryS3:
    """boto3 stand-in for a reachable bucket that keeps its objects in memory."""

    def __init__(self):
        self.objects = {}

    def head_bucket(self, **_kwargs):
        return {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}

    def get_paginator(self, _name):
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}]

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _sid(hours):
    when = NOW + timedelta(hours=hours)
    return when, when.strftime("%Y-%m-%d_%H-%M-%S")


def test_fallback_retention_never_prunes_other_jobs_snapshots(tmp_path, monkeypatch):
    # examples/config/multi-job.json: a local-only job and an S3-only job share
    # one data dir. While S3 is down, the S3-only job's retention (count 2) must
    # prune only its own fallback copies, not the other job's snapshots.
    from backuphelper.destinations.s3 import S3Destination

    data = tmp_path / "data"
    local_job = _fs_job(tmp_path, name="database-hourly", retention={"count": 48})
    for hour in range(10):
        when, sid = _sid(hour)
        run_job(local_job, data_dir=data, instance_name="i", now=when, snapshot_id=sid)
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    offsite_job = Job.model_validate({
        "name": "files-offsite", "sources": [{"type": "env", "name": "env", "whitelist": []}],
        "destinations": [_s3_dest()], "retention": {"count": 2}})
    fallback_ids = []
    for hour in range(10, 13):
        when, sid = _sid(hour)
        result = run_job(offsite_job, data_dir=data, instance_name="i", now=when, snapshot_id=sid)
        assert result.status == "warning"
        fallback_ids.append(sid)

    left = {p.name[: -len(".manifest.json")] for p in data.glob("*.manifest.json")}
    assert {_sid(h)[1] for h in range(10)} <= left          # the other job's 10 survive
    assert left - {_sid(h)[1] for h in range(10)} == set(fallback_ids[1:])  # own count=2


def test_pending_snapshot_is_uploaded_once_s3_is_back(tmp_path, monkeypatch):
    # An S3-only job's fallback copy must not stay in the data dir forever: it
    # would never reach the bucket, and its ageing manifest would turn the
    # container healthcheck unhealthy for good although backups succeed.
    from backuphelper.destinations.s3 import S3Destination
    from backuphelper.healthcheck import is_healthy

    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[_s3_dest()])
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    when, down = _sid(0)
    run_job(job, data_dir=data, instance_name="i", now=when, snapshot_id=down)
    assert (data / f"{down}.offsite-pending.json").exists()

    bucket = _MemoryS3()
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: bucket)
    when, up = _sid(24)
    result = run_job(job, data_dir=data, instance_name="i", now=when, snapshot_id=up)

    assert result.status == "success"
    assert {f"{down}.tar.gz", f"{down}.manifest.json", f"{up}.tar.gz",
            f"{up}.manifest.json"} == set(bucket.objects)
    assert list(data.iterdir()) == []                        # fallback copy + marker gone
    assert is_healthy(data, 26, now=NOW + timedelta(days=3))


def test_keep_local_false_uploads_the_kept_copy_later_and_then_drops_it(tmp_path, monkeypatch):
    from backuphelper.destinations.s3 import S3Destination

    data = tmp_path / "data"
    job = _fs_job(tmp_path, keep_local=False, destinations=[{"type": "local"}, _s3_dest()])
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="p1")
    assert any("next run" in e for e in result.errors)
    assert (data / "p1.tar.gz").exists() and (data / "p1.offsite-pending.json").exists()

    bucket = _MemoryS3()
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: bucket)
    assert run_job(job, data_dir=data, instance_name="i", now=NOW,
                   snapshot_id="p2").status == "success"
    assert {"p1.tar.gz", "p1.manifest.json", "p2.tar.gz", "p2.manifest.json"} == set(bucket.objects)
    assert list(data.iterdir()) == []


def test_keep_local_true_uploads_the_missed_snapshot_and_keeps_it_locally(tmp_path, monkeypatch):
    from backuphelper.destinations.s3 import S3Destination

    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[{"type": "local"}, _s3_dest()])
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="q1")
    bucket = _MemoryS3()
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: bucket)
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="q2")
    assert "q1.manifest.json" in bucket.objects
    assert (data / "q1.tar.gz").exists() and not (data / "q1.offsite-pending.json").exists()


def test_a_failed_pending_upload_keeps_the_snapshot_pending(tmp_path, monkeypatch):
    from backuphelper.destinations.s3 import S3Destination

    data = tmp_path / "data"
    job = _fs_job(tmp_path, destinations=[_s3_dest()])
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="r1")

    real_put = S3Destination.put

    def put(self, local_path, key):
        if key.startswith("r1."):
            raise RuntimeError("slow down")
        real_put(self, local_path, key)

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _MemoryS3())
    monkeypatch.setattr(S3Destination, "put", put)
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="r2")
    assert result.status == "warning"
    assert any(e.startswith("pending snapshot r1:") for e in result.errors)
    assert (data / "r1.tar.gz").exists() and (data / "r1.offsite-pending.json").exists()


def test_a_snapshot_stored_nowhere_is_an_error(tmp_path, monkeypatch):
    # The local put fails (disk full) and S3 is rejected: no copy of this run
    # exists anywhere, so the run must not pass as a mere warning (exit 0, and
    # no alert at notifications.level=errors).
    from backuphelper.cli import run_all_now
    from backuphelper.config.models import RootConfig
    from backuphelper.destinations.local import LocalDestination
    from backuphelper.destinations.s3 import S3Destination

    def disk_full(self, local_path, key):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    monkeypatch.setattr(LocalDestination, "put", disk_full)
    data = tmp_path / "data"
    for destinations in ([{"type": "local"}, _s3_dest()], [_s3_dest()]):
        job = Job.model_validate({"name": "main", "destinations": destinations,
                                  "sources": [{"type": "env", "name": "env", "whitelist": []}]})
        spy = _Spy()
        result = run_job(job, data_dir=data, instance_name="i", notifier=spy, now=NOW,
                         snapshot_id="n1")
        assert result.status == "error" and result.archive is None
        assert any("not stored on any destination" in e for e in result.errors)
        assert spy.events[0].status == "error"
        assert run_all_now(RootConfig(jobs=[job]), data) == 1


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


def test_unconfigured_s3_source_is_skipped(tmp_path):
    # An s3 SOURCE with no bucket means "object storage not configured" — it must
    # be skipped like an unconfigured s3 destination, not attempted and degraded
    # to a partial warning. Lets consumers ship the storage-backup source always
    # and activate it purely by setting the bucket.
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="s3", name="storage", bucket=""))
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="s10")
    assert result.status == "success"
    assert not any("s3" in e for e in result.errors)
    m = read_manifest(sidecar_path(data, "s10"))
    assert "s3" not in {c.kind for c in m.components}


def test_filesystem_without_name_is_restorable(tmp_path):
    # The name the runner looks up at RESTORE must equal the name produce() wrote,
    # or the component is silently skipped. A filesystem source without an explicit
    # "name" used to produce "files" while restore looked up "filesystem".
    from backuphelper.plugins.registry import build_source
    from backuphelper.runner import _spec_component_name

    src = tmp_path / "d"
    src.mkdir()
    (src / "a.txt").write_text("A")
    spec = SourceSpec.model_validate({"type": "filesystem", "path": str(src)})
    staging = tmp_path / "s"
    staging.mkdir()
    produced = build_source(spec.model_dump()).produce(staging)[0].name
    assert _spec_component_name(spec) == produced


def test_spec_component_name_matches_source_for_every_type():
    # The restore lookup name must equal the source's own component name for every
    # source type, including the name-less defaults that used to diverge
    # (filesystem "files" vs "filesystem", postgres "postgres" vs "database").
    from backuphelper.plugins.registry import build_source
    from backuphelper.runner import _spec_component_name

    specs = [
        {"type": "filesystem", "path": "/x"},                 # no name -> "files"
        {"type": "postgres", "host": "h"},                    # no name/db -> "postgres"
        {"type": "postgres", "host": "h", "database": "logto"},
        {"type": "mariadb", "host": "h", "database": "wp"},
        {"type": "s3", "bucket": "b"},                        # no name -> "s3"
        {"type": "env"},                                      # no name -> "env"
        {"type": "filesystem", "name": "uploads", "path": "/x"},
    ]
    for spec_dict in specs:
        spec = SourceSpec.model_validate(spec_dict)
        assert _spec_component_name(spec) == build_source(spec.model_dump()).component_name, spec_dict


def test_disabled_source_is_skipped(tmp_path):
    # A source with "enabled": false is a config-deactivated toggle (e.g. NocoDB's
    # BACKUP_INCLUDE_FILES / BACKUP_DATABASE_DUMP=false) — it must be skipped
    # cleanly, not produced, and must not degrade the run to a warning.
    data = tmp_path / "data"
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("X")
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="filesystem", name="other", path=str(other), enabled=False))
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="d1")
    assert result.status == "success"
    m = read_manifest(sidecar_path(data, "d1"))
    assert "other" not in {c.name for c in m.components}
    assert "uploads" in {c.name for c in m.components}  # enabled sources still run


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


def _pre_restore_spy():
    from backuphelper.plugins.hooks import HookRegistry

    calls = []
    hooks = HookRegistry()
    hooks.register("pre_restore", calls.append)
    return hooks, calls


def test_restore_only_unknown_component_fails_before_touching_anything(tmp_path, caplog):
    # Regression: an --only value that matches no component restored nothing and
    # still reported success ("restore complete", exit 0).
    import logging

    data = tmp_path / "data"
    job = _fs_job(tmp_path)  # components: uploads, env
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="o1")
    shutil.rmtree(tmp_path / "uploads")
    hooks, calls = _pre_restore_spy()
    with caplog.at_level(logging.ERROR, logger="backuphelper.runner"):
        ok = restore_snapshot(job, data_dir=data, snapshot_id="o1", only={"upload"}, hooks=hooks)
    assert ok is False
    assert calls == []                       # the pre_restore gate never ran
    assert not (tmp_path / "uploads").exists()
    assert "'upload'" in caplog.text
    assert "env, uploads" in caplog.text     # lists the valid component names


def test_restore_only_rejects_a_component_that_failed_at_backup(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="filesystem", name="missing", path=str(tmp_path / "nope")))
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="o2")
    hooks, calls = _pre_restore_spy()
    assert restore_snapshot(job, data_dir=data, snapshot_id="o2", only={"missing"},
                            hooks=hooks) is False
    assert calls == []


def test_restore_only_rejects_a_component_without_a_source_in_the_job(tmp_path):
    data = tmp_path / "data"
    run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", now=NOW, snapshot_id="o3")
    other = Job.model_validate({"name": "other", "sources": [{"type": "env", "name": "env"}]})
    hooks, calls = _pre_restore_spy()
    assert restore_snapshot(other, data_dir=data, snapshot_id="o3", only={"uploads"},
                            hooks=hooks) is False
    assert calls == []


def test_restore_only_valid_component_still_restores(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="o4")
    shutil.rmtree(tmp_path / "uploads")
    assert restore_snapshot(job, data_dir=data, snapshot_id="o4", only={"uploads"}) is True
    assert (tmp_path / "uploads" / "a.txt").read_text() == "A"


def test_restore_missing_snapshot_returns_false(tmp_path):
    job = _fs_job(tmp_path)
    assert restore_snapshot(job, data_dir=tmp_path / "data", snapshot_id="nope") is False


def test_restore_hydrates_from_s3_when_local_missing(tmp_path):
    # Disaster-recovery: the local /data volume is gone, but the snapshot still
    # sits in the off-site S3 destination. restore must pull it back and rebuild.
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
            "name": "main",
            "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)}],
            "destinations": [{"type": "local"},
                             {"type": "s3", "bucket": "offsite", "access_key": "k",
                              "secret_key": "s", "region": "eu-central-1", "ensure_bucket": False}],
        })
        run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="h1")

        (data / "h1.tar.gz").unlink()          # simulate loss of the local volume
        (data / "h1.manifest.json").unlink()
        shutil.rmtree(src)                     # and the source, to prove a real rebuild

        ok = restore_snapshot(job, data_dir=data, snapshot_id="h1")

    assert ok is True
    assert (src / "a.txt").read_text() == "A"


def test_remote_snapshot_ids_lists_offsite(tmp_path):
    import boto3
    from moto import mock_aws

    from backuphelper.runner import remote_snapshot_ids

    src = tmp_path / "uploads"
    src.mkdir()
    (src / "a.txt").write_text("A")
    data = tmp_path / "data"
    with mock_aws():
        boto3.client("s3", region_name="eu-central-1", aws_access_key_id="k",
                     aws_secret_access_key="s").create_bucket(
            Bucket="offsite", CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
        job = Job.model_validate({
            "name": "main",
            "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)}],
            "destinations": [{"type": "s3", "bucket": "offsite", "access_key": "k",
                              "secret_key": "s", "region": "eu-central-1", "ensure_bucket": False}],
        })
        run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="r1")
        ids = remote_snapshot_ids(job)
    assert ids == {"r1"}


def test_restore_refuses_corrupt_archive(tmp_path):
    # A destructive restore must abort if the archive fails its sha256 gate,
    # before touching live data.
    src = tmp_path / "uploads"
    src.mkdir()
    (src / "a.txt").write_text("A")
    data = tmp_path / "data"
    job = Job.model_validate({
        "name": "main",
        "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)}],
        "destinations": [{"type": "local"}],
    })
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="c1")
    (data / "c1.tar.gz").write_bytes(b"corrupted")  # tamper — sha256 no longer matches manifest
    assert restore_snapshot(job, data_dir=data, snapshot_id="c1") is False


def test_retention_prunes_old_local_snapshots(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path, retention={"count": 2})
    for i in range(1, 5):
        run_job(job, data_dir=data, instance_name="iam", now=NOW, snapshot_id=f"2026-07-0{i}_03-00-00")
    remaining = sorted(p.name for p in data.glob("*.tar.gz"))
    assert remaining == ["2026-07-03_03-00-00.tar.gz", "2026-07-04_03-00-00.tar.gz"]


def test_validation_errors_never_carry_secret_values(tmp_path, monkeypatch):
    # A numeric secret from a discrete env override (JSON-parsed into an int)
    # fails validation; pydantic used to quote it as input_value=... into the job
    # errors, the alert and the persisted, off-site-uploaded manifest.
    from pydantic import BaseModel

    from backuphelper.config.loader import load_config
    from backuphelper.sources.filesystem import FilesystemSource

    class PluginConfig(BaseModel):  # a plugin's own model, not hiding its input
        api_token: str

    def produce(self, staging_dir):
        PluginConfig.model_validate({"api_token": 55443322})

    monkeypatch.setattr(FilesystemSource, "produce", produce)
    cfg = load_config({
        "BACKUP_CONFIG_JSON": json.dumps({"jobs": [{
            "name": "main",
            "sources": [{"type": "postgres", "host": "db", "database": "app", "user": "app"},
                        {"type": "filesystem", "name": "plugin", "path": str(tmp_path)},
                        {"type": "env", "name": "env", "whitelist": []}],
            "destinations": [{"type": "local"},
                             {"type": "s3", "bucket": "offsite", "access_key": "AK"}]}]}),
        "BACKUP_JOBS__0__SOURCES__0__PASSWORD": "20261006",
        "BACKUP_JOBS__0__DESTINATIONS__1__SECRET_KEY": "90817263",
    })
    data = tmp_path / "data"
    spy = _Spy()
    result = run_job(cfg.jobs[0], data_dir=data, instance_name="i", notifier=spy, now=NOW,
                     snapshot_id="v1")

    manifest = (data / "v1.manifest.json").read_text()
    alert = " ".join(spy.events[0].errors)
    for secret in ("20261006", "90817263", "55443322"):
        assert secret not in manifest and secret not in alert, secret
    assert "password" in manifest and "api_token" in manifest   # the field is still named
    assert any("secret_key" in e for e in result.errors)
