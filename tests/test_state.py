"""Tests for the run-state records the healthcheck reads."""

from datetime import datetime, timezone

from backuphelper.state import (
    RunRecord,
    read_daemon_start,
    read_runs,
    record_daemon_start,
    record_run,
    state_dir,
)

NOW = datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc)


def test_run_records_roundtrip_one_file_per_job(tmp_path):
    record_run(tmp_path, RunRecord("main", "s1", "success", NOW))
    record_run(tmp_path, RunRecord("files", "s2", "error", NOW, ("uploads",)))
    record_run(tmp_path, RunRecord("main", "s3", "warning", NOW))  # replaces main's record
    runs = {r.job: r for r in read_runs(tmp_path)}
    assert runs == {"main": RunRecord("main", "s3", "warning", NOW),
                    "files": RunRecord("files", "s2", "error", NOW, ("uploads",))}
    assert sorted(p.name for p in state_dir(tmp_path).iterdir()) == ["job-files.json",
                                                                     "job-main.json"]


def test_a_job_name_never_escapes_the_state_dir(tmp_path):
    record_run(tmp_path, RunRecord("../../etc/passwd x", "s1", "success", NOW))
    [path] = state_dir(tmp_path).iterdir()
    assert path.name == "job-.._.._etc_passwd_x.json"
    assert read_runs(tmp_path)[0].job == "../../etc/passwd x"


def test_daemon_start_roundtrip_and_absence(tmp_path):
    assert read_daemon_start(tmp_path) is None
    record_daemon_start(tmp_path, now=NOW)
    assert read_daemon_start(tmp_path) == NOW


def test_malformed_records_are_skipped(tmp_path):
    record_run(tmp_path, RunRecord("main", "s1", "success", NOW))
    (state_dir(tmp_path) / "job-broken.json").write_text("[1, 2]")
    (state_dir(tmp_path) / "job-partial.json").write_text('{"job": "x"}')
    (state_dir(tmp_path) / "daemon.json").write_text('{"started_at": 5}')
    assert [r.job for r in read_runs(tmp_path)] == ["main"]
    assert read_daemon_start(tmp_path) is None


def test_writes_leave_no_temp_files(tmp_path):
    for _ in range(3):
        record_run(tmp_path, RunRecord("main", "s1", "success", NOW))
        record_daemon_start(tmp_path, now=NOW)
    assert sorted(p.name for p in state_dir(tmp_path).iterdir()) == ["daemon.json",
                                                                     "job-main.json"]
