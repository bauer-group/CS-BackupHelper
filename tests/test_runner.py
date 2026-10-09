"""Tests for the backup runner (end-to-end orchestration of one job)."""

import json
import shutil
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

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


def test_a_failing_source_makes_the_run_an_error(tmp_path):
    # Regression: a source that failed completely only degraded the run to a
    # warning while another source succeeded - exit 0, a warning alert and a
    # snapshot that silently lacks the source's data.
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(
        SourceSpec(type="filesystem", name="missing", path=str(tmp_path / "does-not-exist"))
    )
    spy = _Spy()
    result = run_job(job, data_dir=data, instance_name="iam", notifier=spy, now=NOW, snapshot_id="s3")
    assert result.status == "error"
    assert any("missing" in e for e in result.errors)
    assert spy.events[0].status == "error"
    assert spy.events[0].message == "snapshot is incomplete - a component failed"
    # the partial snapshot is still stored, and its manifest says what it is
    assert result.archive == data / "s3.tar.gz"
    assert read_manifest(sidecar_path(data, "s3")).status == "error"


class _FailingPgDump:
    """subprocess.run stand-in: pg_dump exits 1 like an unreachable server."""

    def __call__(self, argv, **_kwargs):
        import subprocess

        return subprocess.CompletedProcess(argv, 1, stdout=b"",
                                           stderr=b"connection to server failed")


class _RaisingPluginSource:
    """A consumer plugin source whose backend raises."""

    type = "fakeplugin"

    def __init__(self, spec):
        self.spec = dict(spec)

    @property
    def component_name(self):
        return self.spec.get("name") or self.type

    def produce(self, staging_dir):
        raise RuntimeError("plugin backend answered 500")


class _ForbiddenBucketClient:
    def get_paginator(self, _name):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "ListObjectsV2")


def _break_component(kind, monkeypatch):
    """Make one realistic source fail completely; return its source spec."""
    if kind == "postgres":  # a returned error: pg_dump exits non-zero
        from backuphelper.sources.postgres import PostgresSource

        real_init = PostgresSource.__init__

        def init(self, spec, run=None):
            real_init(self, spec, run=_FailingPgDump())

        monkeypatch.setattr(PostgresSource, "__init__", init)
        return SourceSpec(type="postgres", name="database", host="db", database="app")
    if kind == "plugin":  # a raised error: the plugin's backend fails
        from backuphelper.plugins import registry

        monkeypatch.setitem(registry.BUILTIN_SOURCES, "fakeplugin", _RaisingPluginSource)
        return SourceSpec(type="fakeplugin", name="app-export")
    # an S3 source whose bucket rejects the credentials
    from backuphelper.sources.s3_bucket import S3BucketSource

    monkeypatch.setattr(S3BucketSource, "_build_client", lambda self: _ForbiddenBucketClient())
    return SourceSpec(type="s3", name="attachments", bucket="attachments")


@pytest.mark.parametrize("kind", ["postgres", "plugin", "s3"])
def test_a_completely_failed_component_fails_the_run(tmp_path, monkeypatch, kind):
    # A snapshot without its database must not look healthy: exit code 1, an
    # error alert and status "error" in the manifest - although the filesystem
    # and env components were backed up and the snapshot was stored.
    from backuphelper.cli import run_all_now
    from backuphelper.config.models import RootConfig

    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(_break_component(kind, monkeypatch))
    spy = _Spy()
    result = run_job(job, data_dir=data, instance_name="i", notifier=spy, now=NOW,
                     snapshot_id="e1")
    assert result.status == "error"
    assert [e.status for e in spy.events] == ["error"]
    manifest = read_manifest(sidecar_path(data, "e1"))
    assert manifest.status == "error"
    failed = [c for c in manifest.components if c.error]
    assert len(failed) == 1 and failed[0].size == 0
    assert {c.name for c in manifest.components if not c.error} == {"uploads", "env"}
    assert run_all_now(RootConfig(jobs=[job]), data) == 1


def test_a_component_without_output_is_a_failed_component(tmp_path, monkeypatch):
    # A source that returns a component with neither a file nor an error text
    # produced nothing: it was recorded with error=None, so restore and every
    # "error-free?" check treated the empty component as good.
    from backuphelper.sources.base import StagedComponent
    from backuphelper.sources.filesystem import FilesystemSource

    real_produce = FilesystemSource.produce

    def produce(self, staging_dir):
        if self.cfg.name != "empty":
            return real_produce(self, staging_dir)
        return [StagedComponent(name="empty", kind="filesystem", path=None)]

    monkeypatch.setattr(FilesystemSource, "produce", produce)
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="filesystem", name="empty", path=str(tmp_path)))
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="e2")
    assert result.status == "error"
    comp = {c.name: c for c in read_manifest(sidecar_path(data, "e2")).components}["empty"]
    assert comp.error == "no output"
    assert "empty: no output" in result.errors


def test_source_warnings_keep_the_run_a_warning(tmp_path, monkeypatch):
    # Non-fatal source warnings (skipped files) are not a failed component.
    from backuphelper.sources.base import StagedComponent
    from backuphelper.sources.filesystem import FilesystemSource

    real_produce = FilesystemSource.produce

    def produce(self, staging_dir):
        [staged] = real_produce(self, staging_dir)
        return [StagedComponent(name=staged.name, kind=staged.kind, path=staged.path,
                                metadata={**staged.metadata, "warnings": ["x.txt (unreadable)"]})]

    monkeypatch.setattr(FilesystemSource, "produce", produce)
    data = tmp_path / "data"
    spy = _Spy()
    result = run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", notifier=spy,
                     now=NOW, snapshot_id="w1")
    assert result.status == "warning"
    assert spy.events[0].status == "warning"
    assert spy.events[0].message == "snapshot completed with warnings"
    assert read_manifest(sidecar_path(data, "w1")).status == "warning"


def test_a_successful_snapshot_records_success_in_its_manifest(tmp_path):
    data = tmp_path / "data"
    run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", now=NOW, snapshot_id="ok1")
    assert read_manifest(sidecar_path(data, "ok1")).status == "success"
    with tarfile.open(data / "ok1.tar.gz", "r:gz") as tar:  # the embedded copy agrees
        embedded = json.loads(tar.extractfile("manifest.json").read())
    assert embedded["status"] == "success"


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

    assert result.status == "error"
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
    assert result.status == "error"
    comp = {c.name: c for c in read_manifest(sidecar_path(data, "f2")).components}["nocodb"]
    assert comp.kind == "nocodb" and comp.size == 0 and comp.sha256 == "" and comp.error


def test_an_unreadable_subdirectory_keeps_the_readable_rest_and_warns(tmp_path, monkeypatch):
    # End to end: an unreadable directory below a filesystem source used to vanish
    # silently. The readable rest is backed up and restorable, the job warns.
    import os

    real_scandir = os.scandir
    locked = tmp_path / "uploads" / "locked"

    def scandir(path="."):
        if str(path) == str(locked):
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    locked.mkdir()
    (locked / "secret.txt").write_text("S")
    monkeypatch.setattr(os, "scandir", scandir)
    spy = _Spy()
    result = run_job(job, data_dir=data, instance_name="i", notifier=spy, now=NOW,
                     snapshot_id="f4")
    assert result.status == "warning"
    assert any(e.startswith("uploads: incomplete, skipped: locked/") for e in result.errors)
    assert spy.events[0].status == "warning"
    comp = {c.name: c for c in read_manifest(sidecar_path(data, "f4")).components}["uploads"]
    assert comp.error is None and comp.size > 0 and len(comp.sha256) == 64
    assert comp.metadata["file_count"] == 1
    assert comp.metadata["warnings"] == ["locked/ (directory not readable: Permission denied)"]
    assert read_manifest(sidecar_path(data, "f4")).status == "warning"
    monkeypatch.setattr(os, "scandir", real_scandir)
    (tmp_path / "uploads" / "a.txt").unlink()
    assert restore_snapshot(job, data_dir=data, snapshot_id="f4", only=["uploads"])
    assert (tmp_path / "uploads" / "a.txt").read_text() == "A"


def test_an_unreadable_root_is_recorded_as_a_failed_component(tmp_path, monkeypatch):
    # An unreadable path-group root leaves nothing to back up: errored component.
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
    assert result.status == "error"
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


def _refusing_hooks():
    from backuphelper.plugins.hooks import HookRegistry

    def refuse(_ctx):
        raise RuntimeError("app refused to quiesce")

    hooks = HookRegistry()
    hooks.register("pre_backup", refuse)
    return hooks


def _ticking_clock(monkeypatch, *ticks):
    """The runner's monotonic clock returns ``ticks`` in turn."""
    from backuphelper import runner

    readings = iter(ticks)
    monkeypatch.setattr(runner, "_clock", lambda: next(readings))


def test_the_alert_reports_how_long_the_run_took(tmp_path, monkeypatch):
    # Regression: the duration was "finished - started" with both set to the
    # run's start (``now``), so every alert said 0 s and the mail left the
    # duration out. It is measured on a monotonic clock now, independent of
    # ``now``, which only names the snapshot and dates the run.
    _ticking_clock(monkeypatch, 100.0, 112.5)
    spy = _Spy()
    run_job(_fs_job(tmp_path), data_dir=tmp_path / "data", instance_name="i", notifier=spy,
            now=NOW, snapshot_id="d1")
    [event] = spy.events
    assert event.duration_seconds == 12.5


def test_an_aborted_run_reports_how_long_it_ran(tmp_path, monkeypatch):
    _ticking_clock(monkeypatch, 50.0, 53.25)
    spy = _Spy()
    with pytest.raises(RuntimeError, match="quiesce"):
        run_job(_fs_job(tmp_path), data_dir=tmp_path / "data", instance_name="i",
                notifier=spy, now=NOW, snapshot_id="d2", hooks=_refusing_hooks())
    [event] = spy.events
    assert event.duration_seconds == 3.25


def test_a_real_run_reports_a_positive_duration(tmp_path, monkeypatch):
    # Without a pinned clock: the time the sources take shows up in the alert.
    import time

    from backuphelper.sources.filesystem import FilesystemSource

    produce = FilesystemSource.produce

    def slow_produce(self, staging):
        time.sleep(0.05)
        return produce(self, staging)

    monkeypatch.setattr(FilesystemSource, "produce", slow_produce)
    spy = _Spy()
    run_job(_fs_job(tmp_path), data_dir=tmp_path / "data", instance_name="i", notifier=spy,
            now=NOW, snapshot_id="d3")
    assert spy.events[0].duration_seconds >= 0.05


def test_an_aborted_run_turns_the_healthcheck_unhealthy(tmp_path):
    # Regression: a run that aborts before it writes a manifest left the
    # previous good snapshot as "the last backup" - healthy for another day.
    from backuphelper.healthcheck import is_healthy

    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="a1")
    assert is_healthy(data, 26, now=NOW + timedelta(hours=1))
    with pytest.raises(RuntimeError, match="quiesce"):
        run_job(job, data_dir=data, instance_name="i", now=NOW + timedelta(hours=1),
                snapshot_id="a2", hooks=_refusing_hooks())
    assert not is_healthy(data, 26, now=NOW + timedelta(hours=2))


def test_a_run_with_a_failed_component_turns_the_healthcheck_unhealthy(tmp_path):
    # Regression: the fresh snapshot without its failed component kept the
    # container healthy. A newer complete snapshot turns it healthy again.
    from backuphelper.healthcheck import is_healthy

    data = tmp_path / "data"
    good = _fs_job(tmp_path)
    broken = good.model_copy(deep=True)
    broken.sources.append(SourceSpec(type="filesystem", name="missing",
                                     path=str(tmp_path / "nope")))
    run_job(broken, data_dir=data, instance_name="i", now=NOW, snapshot_id="h1")
    assert not is_healthy(data, 26, now=NOW + timedelta(hours=1))
    run_job(good, data_dir=data, instance_name="i", now=NOW + timedelta(hours=2),
            snapshot_id="h2")
    assert is_healthy(data, 26, now=NOW + timedelta(hours=3))


def test_keep_local_false_keeps_staleness_detectable(tmp_path, monkeypatch):
    # Regression: keep_local=false leaves no local manifest, so the probe sat in
    # its grace for good and never noticed that backups had stopped.
    from backuphelper.destinations.s3 import S3Destination
    from backuphelper.healthcheck import is_healthy

    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _MemoryS3())
    data = tmp_path / "data"
    job = _fs_job(tmp_path, keep_local=False, destinations=[{"type": "local"}, _s3_dest()])
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="k5")
    assert result.status == "success"
    assert not list(data.glob("*.manifest.json"))  # nothing local left
    assert is_healthy(data, 26, now=NOW + timedelta(hours=1))
    assert not is_healthy(data, 26, now=NOW + timedelta(hours=27))


def test_every_run_leaves_a_record_for_the_healthcheck(tmp_path):
    data = tmp_path / "data"
    job = _fs_job(tmp_path)
    job.sources.append(SourceSpec(type="filesystem", name="missing", path=str(tmp_path / "nope")))
    run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="r9")
    record = json.loads((data / ".state" / "job-main.json").read_text())
    assert record == {"version": 1, "job": "main", "snapshot_id": "r9", "status": "error",
                      "started_at": NOW.isoformat(), "failed_components": ["missing"]}


def test_a_failing_run_record_never_fails_the_run(tmp_path, monkeypatch, caplog):
    import backuphelper.runner as runner_module

    def disk_full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runner_module, "record_run", disk_full)
    data = tmp_path / "data"
    spy = _Spy()
    with caplog.at_level("ERROR", logger="backuphelper.runner"):
        result = run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", notifier=spy,
                         now=NOW, snapshot_id="r10")
    assert result.status == "success" and spy.events[0].status == "success"
    assert "could not record the outcome of job main" in caplog.text


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


def _freeze_runs_at(monkeypatch, when):
    """Every run starts at ``when`` - all jobs of one --now fire in that second."""
    import datetime as dt

    import backuphelper.runner as runner_module

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz else when.replace(tzinfo=None)

    monkeypatch.setattr(runner_module, "datetime", Frozen)


def _env_job(name, **over):
    return {"name": name, "sources": [{"type": "env", "name": "env", "whitelist": []}],
            "destinations": [{"type": "local"}], **over}


def _left(data):
    return sorted(p.name[: -len(".manifest.json")] for p in data.glob("*.manifest.json"))


def test_local_retention_of_one_job_never_prunes_another_jobs_snapshots(tmp_path, monkeypatch):
    # Regression: the retention of a job with a local destination worked on the
    # whole data dir, so a job with count 2 also pruned the other job's
    # snapshots down to the newest two - whatever the other job's own policy.
    from backuphelper.cli import run_all_now
    from backuphelper.config.models import RootConfig

    data = tmp_path / "data"
    cfg = RootConfig.model_validate({"jobs": [_env_job("db", retention={"count": 2}),
                                              _env_job("files", retention={"count": 10})]})
    for hour in range(5):
        _freeze_runs_at(monkeypatch, _sid(hour)[0])
        assert run_all_now(cfg, data) == 0
    assert _left(data) == sorted([f"{_sid(h)[1]}_db" for h in (3, 4)]
                                 + [f"{_sid(h)[1]}_files" for h in range(5)])


def test_s3_retention_of_one_job_never_prunes_another_jobs_snapshots(tmp_path, monkeypatch):
    # Two S3-only jobs sharing one bucket and prefix: each prunes its own.
    from backuphelper.cli import run_all_now
    from backuphelper.config.models import RootConfig
    from backuphelper.destinations.s3 import S3Destination

    bucket = _MemoryS3()
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: bucket)
    cfg = RootConfig.model_validate({"jobs": [
        _env_job("db", destinations=[_s3_dest()], retention={"count": 1}),
        _env_job("files", destinations=[_s3_dest()], retention={"count": 3})]})
    for hour in range(4):
        _freeze_runs_at(monkeypatch, _sid(hour)[0])
        assert run_all_now(cfg, tmp_path / "data") == 0
    remote = sorted(k[: -len(".manifest.json")] for k in bucket.objects
                    if k.endswith(".manifest.json"))
    assert remote == sorted([f"{_sid(3)[1]}_db"] + [f"{_sid(h)[1]}_files" for h in (1, 2, 3)])


def test_old_plain_snapshots_go_to_the_first_job_that_stores_there(tmp_path, monkeypatch):
    # Snapshots from before job-scoped ids have plain ids. In a multi-job config
    # they count as the first job's that stores in that place - except a
    # fallback copy, which belongs to the job its pending marker names.
    from backuphelper.cli import run_all_now
    from backuphelper.config.models import RootConfig
    from backuphelper.destinations.s3 import S3Destination

    data = tmp_path / "data"
    db = _env_job("db", retention={"count": 2})
    files = _env_job("files", destinations=[_s3_dest()], retention={"count": 5})
    for hour in range(3):  # 1.7.7-style plain ids of the local job
        run_job(Job.model_validate(db), data_dir=data, instance_name="i", now=_sid(hour)[0],
                snapshot_id=_sid(hour)[1])
    monkeypatch.setattr(S3Destination, "_build_client", lambda self: _ForbiddenS3())
    run_job(Job.model_validate(files), data_dir=data, instance_name="i",  # a fallback copy
            now=_sid(3)[0], snapshot_id=_sid(3)[1])
    assert (data / f"{_sid(3)[1]}.offsite-pending.json").exists()

    cfg = RootConfig.model_validate({"jobs": [db, files]})
    for hour in (4, 5):
        _freeze_runs_at(monkeypatch, _sid(hour)[0])
        run_all_now(cfg, data)
    left = _left(data)
    assert [s for s in left if s.endswith("_db")] == [f"{_sid(4)[1]}_db", f"{_sid(5)[1]}_db"]
    assert _sid(3)[1] in left  # the other job's pending fallback copy survives
    assert not any(_sid(h)[1] in left for h in range(3))  # the old plain ones were db's


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
    assert [p.name for p in data.iterdir()] == [".state"]  # fallback copy + marker gone
    assert is_healthy(data, 26, now=NOW + timedelta(hours=25))  # fresh from the 'up' run


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
    assert [p.name for p in data.iterdir()] == [".state"]  # only the run record is left


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


def test_a_source_disabled_by_a_numeric_env_override_stays_disabled(tmp_path):
    # A discrete override passes a source's own keys as text now; ENABLED=0
    # must still switch the source off as the number 0 did.
    from backuphelper.config.loader import load_config

    cfg = load_config({
        "BACKUP_CONFIG_JSON": json.dumps({"jobs": [{
            "name": "main",
            "sources": [{"type": "env", "name": "env", "whitelist": []},
                        {"type": "filesystem", "name": "gone", "path": str(tmp_path / "nope")}]}]}),
        "BACKUP_JOBS__0__SOURCES__1__ENABLED": "0"})
    result = run_job(cfg.jobs[0], data_dir=tmp_path / "data", instance_name="i", now=NOW,
                     snapshot_id="d0")
    assert result.status == "success"
    assert [c.name for c in result.components] == ["env"]


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
    # a disabled source is not a component: had it run, its missing path would fail it
    job.sources.append(SourceSpec(type="filesystem", name="gone", path=str(tmp_path / "nope"),
                                  enabled=False))
    result = run_job(job, data_dir=data, instance_name="i", now=NOW, snapshot_id="d1")
    assert result.status == "success" and result.errors == []
    m = read_manifest(sidecar_path(data, "d1"))
    assert "other" not in {c.name for c in m.components}
    assert "uploads" in {c.name for c in m.components}  # enabled sources still run


def test_run_leaves_no_work_artifacts_in_data_dir(tmp_path):
    data = tmp_path / "data"
    run_job(_fs_job(tmp_path), data_dir=data, instance_name="i", now=NOW, snapshot_id="s9")
    top = sorted(p.name for p in data.iterdir())
    assert top == [".state", "s9.manifest.json", "s9.tar.gz"]  # no leftover .work dir


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
    # A numeric secret (a JSON number where the source or destination expects
    # text) fails validation; pydantic used to quote it as input_value=... into
    # the job errors, the alert and the persisted, off-site-uploaded manifest.
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
            "sources": [{"type": "postgres", "host": "db", "database": "app", "user": "app",
                         "password": 20261006},
                        {"type": "filesystem", "name": "plugin", "path": str(tmp_path)},
                        {"type": "env", "name": "env", "whitelist": []}],
            "destinations": [{"type": "local"},
                             {"type": "s3", "bucket": "offsite", "access_key": "AK",
                              "secret_key": 90817263}]}]}),
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
