"""Hermetic CLI tests via Typer's CliRunner (filesystem source, local dest)."""

import json

from typer.testing import CliRunner

from backuphelper.cli import app

runner = CliRunner()


# Dummy credentials are assembled at runtime: credential-looking literals in the
# source trip secret scanners (GitGuardian) although nothing here is real.
def _fake(label: str) -> str:
    return f"FAKE-{label}"


def _env(tmp_path):
    src = tmp_path / "uploads"
    src.mkdir()
    (src / "a.txt").write_text("A")
    cfg = {
        "instance_name": "iam",
        "jobs": [{"name": "main",
                  "sources": [{"type": "filesystem", "name": "uploads", "path": str(src)}],
                  "destinations": [{"type": "local"}],
                  "notifications": {"channels": []}}],
    }
    return {"BACKUP_CONFIG_JSON": json.dumps(cfg), "BACKUP_DATA_DIR": str(tmp_path / "data")}


def test_create_then_list_then_verify(tmp_path):
    env = _env(tmp_path)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0

    listed = runner.invoke(app, ["list"], env=env)
    assert listed.exit_code == 0 and "uploads" not in listed.stdout  # lists snapshot ids
    sid = listed.stdout.split()[0]

    verified = runner.invoke(app, ["verify", sid], env=env)
    assert verified.exit_code == 0 and verified.stdout.startswith("OK")


def _with_job(env, **job_over):
    cfg = json.loads(env["BACKUP_CONFIG_JSON"])
    cfg["jobs"][0].update(job_over)
    return {**env, "BACKUP_CONFIG_JSON": json.dumps(cfg)}


def test_create_and_now_exit_non_zero_when_a_component_fails(tmp_path):
    # Regression: one failed source (here a missing path) next to a good one
    # exited 0, so cron wrappers and CI took a snapshot without that data as fine.
    env = _env(tmp_path)
    cfg = json.loads(env["BACKUP_CONFIG_JSON"])
    sources = cfg["jobs"][0]["sources"] + [
        {"type": "filesystem", "name": "missing", "path": str(tmp_path / "nope")}]
    env = _with_job(env, sources=sources)

    assert runner.invoke(app, ["create"], env=env).exit_code == 1
    assert runner.invoke(app, ["--now"], env=env).exit_code == 1

    # the partial snapshot is kept and `show` names its status
    sid = runner.invoke(app, ["list"], env=env).stdout.split()[0]
    shown = json.loads(runner.invoke(app, ["show", sid], env=env).stdout)
    assert shown["status"] == "error"
    assert [c["name"] for c in shown["components"] if c["error"]] == ["missing"]


def test_create_exits_zero_on_a_warning(tmp_path):
    # keep_local=false without a configured S3 bucket keeps the local copy and
    # warns - a genuine warning, not a failure.
    env = _with_job(_env(tmp_path), keep_local=False,
                    destinations=[{"type": "local"}, {"type": "s3", "bucket": ""}])
    assert runner.invoke(app, ["create"], env=env).exit_code == 0


def test_config_print_redacted_masks_secrets(tmp_path):
    env = _env(tmp_path)
    env["BACKUP_CONFIG_JSON"] = json.dumps({
        "jobs": [{"name": "j", "sources": [{"type": "postgres", "password": "hunter2"}]}]
    })
    out = runner.invoke(app, ["config", "--redacted"], env=env)
    assert out.exit_code == 0
    assert "hunter2" not in out.stdout


def test_config_print_redacts_by_default(tmp_path):
    # Safe-by-default: bare `config` must NOT leak secrets in cleartext.
    env = _env(tmp_path)
    env["BACKUP_CONFIG_JSON"] = json.dumps({
        "jobs": [{"name": "j", "sources": [{"type": "postgres", "password": "hunter2"}]}]
    })
    out = runner.invoke(app, ["config"], env=env)
    assert out.exit_code == 0
    assert "hunter2" not in out.stdout


def test_config_redacts_s3_and_notification_credentials(tmp_path):
    # Regression: the S3 secret_key was printed in clear text by the (redacted
    # by default) config command, because its key does not END in "secret".
    env = _env(tmp_path)
    env["BACKUP_CONFIG_JSON"] = json.dumps({"jobs": [{
        "name": "j",
        "sources": [{"type": "s3", "bucket": "src", "access_key": _fake("SRC-AK"),
                     "secret_key": _fake("SRC-SK")},
                    {"type": "nocodb", "api_token": _fake("PLUGIN-TOKEN"),
                     "client_secret": _fake("PLUGIN-CS"), "private_key": _fake("PLUGIN-PK"),
                     "passphrase": 918273, "port": 5432}],
        "destinations": [{"type": "s3", "bucket": "offsite", "access_key": _fake("DST-AK"),
                          "secret_key": _fake("DST-SK")}],
        "notifications": {"email": {"password": _fake("SMTP-PW"), "recipients": ["ops@x"]},
                          "webhook": {"secret": _fake("HMAC")},
                          "ntfy": {"token": _fake("NTFY")},
                          "slack": {"url": "https://hooks.slack.com/services/T0/B0/"
                                           + _fake("SLACK-HOOK")},
                          "teams": {"url": "https://prod.logic.azure.com/wf?sp=1&sig="
                                           + _fake("TEAMS-SIG")},
                          "healthchecks": {"url": "https://hc-ping.com/" + _fake("HC-UUID")}},
    }]})
    out = runner.invoke(app, ["config"], env=env)
    assert out.exit_code == 0
    for label in ("DST-SK", "DST-AK", "SRC-AK", "SRC-SK", "PLUGIN-TOKEN", "PLUGIN-CS",
                  "PLUGIN-PK", "SMTP-PW", "HMAC", "NTFY", "SLACK-HOOK", "TEAMS-SIG", "HC-UUID"):
        assert _fake(label) not in out.stdout, label
    assert "918273" not in out.stdout
    # Structural redaction: a numeric credential is masked without breaking the
    # JSON (a text regex would turn `"passphrase": 918273,` into invalid JSON).
    printed = json.loads(out.stdout)
    job = printed["jobs"][0]
    plugin = job["sources"][1]
    assert plugin["passphrase"] == "***" and plugin["port"] == 5432
    dest = job["destinations"][0]
    assert dest["bucket"] == "offsite" and dest["secret_key"] == "***"
    assert job["notifications"]["email"]["recipients"] == ["ops@x"]
    assert job["notifications"]["slack"]["url"] == "https://hooks.slack.com/***"


def test_config_show_secrets_reveals(tmp_path):
    env = _env(tmp_path)
    env["BACKUP_CONFIG_JSON"] = json.dumps({
        "jobs": [{"name": "j", "sources": [{"type": "postgres", "password": "hunter2"}]}]
    })
    out = runner.invoke(app, ["config", "--show-secrets"], env=env)
    assert out.exit_code == 0
    assert "hunter2" in out.stdout


def test_healthcheck_on_a_freshly_started_daemon_is_healthy(tmp_path, monkeypatch):
    # `docker compose up --wait` on a fresh stack relies on this: no backup has
    # run yet, but the daemon recorded its start, which opens the grace.
    from apscheduler.schedulers.blocking import BlockingScheduler

    import backuphelper.scheduler as scheduler
    from backuphelper.cli import run_daemon
    from backuphelper.config.loader import load_config

    env = _env(tmp_path)
    monkeypatch.setattr(BlockingScheduler, "start", lambda self: None)  # return at once
    monkeypatch.setattr(scheduler, "install_signal_drain", lambda sched: None)
    run_daemon(load_config(env), tmp_path / "data")
    out = runner.invoke(app, ["healthcheck"], env=env)
    assert out.exit_code == 0
    assert out.stdout.startswith("healthy: no backup has run yet")


def test_healthcheck_without_a_backup_or_a_daemon_start_is_unhealthy(tmp_path):
    (tmp_path / "empty").mkdir()
    out = runner.invoke(app, ["healthcheck"], env={"BACKUP_DATA_DIR": str(tmp_path / "empty")})
    assert out.exit_code == 1 and out.stdout.startswith("unhealthy:")


def test_healthcheck_reports_a_last_run_with_a_failed_component(tmp_path):
    import time

    env = _env(tmp_path)
    cfg = json.loads(env["BACKUP_CONFIG_JSON"])
    good_sources = cfg["jobs"][0]["sources"]
    broken = _with_job(env, sources=good_sources + [
        {"type": "filesystem", "name": "missing", "path": str(tmp_path / "nope")}])
    assert runner.invoke(app, ["create"], env=broken).exit_code == 1
    out = runner.invoke(app, ["healthcheck"], env=env)
    assert out.exit_code == 1
    assert "the last backup failed" in out.stdout and "missing" in out.stdout

    time.sleep(1.1)  # the next snapshot gets a new id (ids have one-second resolution)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0
    assert runner.invoke(app, ["healthcheck"], env=env).exit_code == 0


def test_verify_missing_snapshot_fails(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    assert runner.invoke(app, ["verify", "2000-01-01_00-00-00"], env=env).exit_code == 2


def test_restore_roundtrip_via_cli(tmp_path):
    import shutil

    env = _env(tmp_path)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0
    sid = runner.invoke(app, ["list"], env=env).stdout.split()[0]

    shutil.rmtree(tmp_path / "uploads")  # data loss
    result = runner.invoke(app, ["restore", sid, "--force"], env=env)
    assert result.exit_code == 0
    assert (tmp_path / "uploads" / "a.txt").read_text() == "A"


def test_restore_only_unknown_component_fails_and_lists_valid_names(tmp_path):
    env = _env(tmp_path)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0
    sid = runner.invoke(app, ["list"], env=env).stdout.split()[0]

    import shutil

    shutil.rmtree(tmp_path / "uploads")
    result = runner.invoke(app, ["restore", sid, "--force", "--only", "upload"], env=env)
    assert result.exit_code == 1
    assert "restore complete" not in result.output
    assert "uploads" in result.output  # the valid component name is shown
    assert not (tmp_path / "uploads").exists()


def test_download_copies_artifact(tmp_path):
    env = _env(tmp_path)
    runner.invoke(app, ["create"], env=env)
    sid = runner.invoke(app, ["list"], env=env).stdout.split()[0]
    out = tmp_path / "out"
    assert runner.invoke(app, ["download", sid, str(out)], env=env).exit_code == 0
    assert (out / f"{sid}.tar.gz").exists() and (out / f"{sid}.manifest.json").exists()


# ── snapshot ids: job-scoped when several jobs share the data dir ────────────
def _freeze_runs_at(monkeypatch, when):
    """Every run starts at ``when``, as two jobs fired in the same second."""
    import datetime as dt

    import backuphelper.runner as runner_module

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz else when.replace(tzinfo=None)

    monkeypatch.setattr(runner_module, "datetime", Frozen)


def _two_jobs_env(tmp_path):
    for name, content in (("db", "D"), ("files", "F")):
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.txt").write_text(content)
    cfg = {"instance_name": "i", "jobs": [
        {"name": name, "sources": [{"type": "filesystem", "name": name, "path": str(tmp_path / name)}],
         "destinations": [{"type": "local"}]} for name in ("db", "files")]}
    return {"BACKUP_CONFIG_JSON": json.dumps(cfg), "BACKUP_DATA_DIR": str(tmp_path / "data")}


def _ids(env):
    return [line.split()[0] for line in runner.invoke(app, ["list"], env=env).stdout.splitlines()]


def test_jobs_that_start_in_the_same_second_keep_their_own_snapshots(tmp_path, monkeypatch):
    # Regression: ids carried no job name, so the second job wrote the same
    # <id>.tar.gz and <id>.manifest.json and replaced the first job's snapshot.
    import shutil
    from datetime import datetime, timezone

    _freeze_runs_at(monkeypatch, datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc))
    env = _two_jobs_env(tmp_path)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0

    assert _ids(env) == ["2026-07-06_03-00-00_db", "2026-07-06_03-00-00_files"]
    for name in ("db", "files"):
        sid = f"2026-07-06_03-00-00_{name}"
        shown = json.loads(runner.invoke(app, ["show", sid], env=env).stdout)
        assert shown["snapshot_id"] == sid and [c["name"] for c in shown["components"]] == [name]
        assert runner.invoke(app, ["verify", sid], env=env).stdout.startswith("OK")
    shutil.rmtree(tmp_path / "db")
    restored = runner.invoke(app, ["restore", "2026-07-06_03-00-00_db", "--job", "db", "--force"],
                             env=env)
    assert restored.exit_code == 0 and (tmp_path / "db" / "db.txt").read_text() == "D"


def test_restore_without_job_picks_the_job_a_scoped_id_names(tmp_path, monkeypatch):
    # Without --job, restore used the first job: a later job's snapshot found
    # no matching source, every component was skipped and it still reported
    # "restore complete".
    import shutil
    from datetime import datetime, timezone

    _freeze_runs_at(monkeypatch, datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc))
    env = _two_jobs_env(tmp_path)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0
    shutil.rmtree(tmp_path / "files")
    restored = runner.invoke(app, ["restore", "2026-07-06_03-00-00_files", "--force"], env=env)
    assert restored.exit_code == 0
    assert (tmp_path / "files" / "files.txt").read_text() == "F"


def test_a_single_job_keeps_plain_timestamp_ids(tmp_path, monkeypatch):
    # The backup round-trip gate of every consumer matches ^YYYY-MM-DD_HH-MM-SS$.
    from datetime import datetime, timezone

    _freeze_runs_at(monkeypatch, datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc))
    env = _env(tmp_path)
    assert runner.invoke(app, ["create"], env=env).exit_code == 0
    assert runner.invoke(app, ["list"], env=env).stdout.split()[0] == "2026-07-06_03-00-00"


def test_prune_without_job_prunes_each_jobs_own_snapshots_by_its_own_policy(tmp_path,
                                                                            monkeypatch):
    # Regression: prune applied the FIRST job's retention to every snapshot in
    # the data dir, so the other job's snapshots were pruned by a foreign policy.
    from datetime import datetime, timezone

    env = _two_jobs_env(tmp_path)
    for day in (1, 2, 3):  # the runs keep everything (default count 14)
        _freeze_runs_at(monkeypatch, datetime(2026, 7, day, 3, 0, 0, tzinfo=timezone.utc))
        assert runner.invoke(app, ["create"], env=env).exit_code == 0
    cfg = json.loads(env["BACKUP_CONFIG_JSON"])
    cfg["jobs"][0]["retention"] = {"count": 1}
    cfg["jobs"][1]["retention"] = {"count": 2}
    out = runner.invoke(app, ["prune"], env={**env, "BACKUP_CONFIG_JSON": json.dumps(cfg)})
    assert out.exit_code == 0
    assert _ids(env) == ["2026-07-02_03-00-00_files", "2026-07-03_03-00-00_db",
                         "2026-07-03_03-00-00_files"]


def test_prune_with_job_prunes_only_that_jobs_snapshots(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    env = _two_jobs_env(tmp_path)
    for day in (1, 2, 3):
        _freeze_runs_at(monkeypatch, datetime(2026, 7, day, 3, 0, 0, tzinfo=timezone.utc))
        assert runner.invoke(app, ["create"], env=env).exit_code == 0
    dry = runner.invoke(app, ["prune", "--job", "files", "--keep", "1", "--dry-run"], env=env)
    assert dry.exit_code == 0
    assert dry.stdout.splitlines() == ["would prune 2026-07-01_03-00-00_files",
                                       "would prune 2026-07-02_03-00-00_files"]
    assert runner.invoke(app, ["prune", "--job", "files", "--keep", "1"], env=env).exit_code == 0
    assert _ids(env) == ["2026-07-01_03-00-00_db", "2026-07-02_03-00-00_db",
                         "2026-07-03_03-00-00_db", "2026-07-03_03-00-00_files"]
    unknown = runner.invoke(app, ["prune", "--job", "nope"], env=env)
    assert unknown.exit_code == 1 and "no job named 'nope'" in unknown.stdout


def test_prune_of_a_single_job_is_unchanged(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    env = _env(tmp_path)
    for day in (1, 2, 3, 4):
        _freeze_runs_at(monkeypatch, datetime(2026, 7, day, 3, 0, 0, tzinfo=timezone.utc))
        assert runner.invoke(app, ["create"], env=env).exit_code == 0
    dry = runner.invoke(app, ["prune", "--keep", "2", "--dry-run"], env=env)
    assert dry.stdout.splitlines() == ["would prune 2026-07-01_03-00-00",
                                       "would prune 2026-07-02_03-00-00"]
    assert runner.invoke(app, ["prune", "--keep", "2"], env=env).exit_code == 0
    assert _ids(env) == ["2026-07-03_03-00-00", "2026-07-04_03-00-00"]


def test_old_plain_and_new_scoped_snapshots_live_side_by_side(tmp_path, monkeypatch):
    # A deployment that grows from one job to two keeps its old snapshots
    # listable, verifiable and restorable next to the new job-scoped ones.
    import shutil
    from datetime import datetime, timezone

    env = _two_jobs_env(tmp_path)
    single = json.loads(env["BACKUP_CONFIG_JSON"])
    single["jobs"] = single["jobs"][:1]
    _freeze_runs_at(monkeypatch, datetime(2026, 7, 5, 3, 0, 0, tzinfo=timezone.utc))
    assert runner.invoke(app, ["create"], env={**env, "BACKUP_CONFIG_JSON": json.dumps(single)}
                         ).exit_code == 0
    _freeze_runs_at(monkeypatch, datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc))
    assert runner.invoke(app, ["create"], env=env).exit_code == 0

    assert _ids(env) == ["2026-07-05_03-00-00", "2026-07-06_03-00-00_db",
                         "2026-07-06_03-00-00_files"]
    for sid in _ids(env):
        assert runner.invoke(app, ["verify", sid], env=env).exit_code == 0, sid
    shutil.rmtree(tmp_path / "db")
    assert runner.invoke(app, ["restore", "2026-07-05_03-00-00", "--force"], env=env).exit_code == 0
    assert (tmp_path / "db" / "db.txt").read_text() == "D"


# ── healthcheck: per job ─────────────────────────────────────────────────────
def _record(dd, job, hours_ago, status="success", failed=()):
    from datetime import datetime, timedelta, timezone

    from backuphelper.state import RunRecord, record_run

    record_run(dd, RunRecord(job=job, snapshot_id=f"{job}-{hours_ago}", status=status,
                             started_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                             failed_components=tuple(failed)))


def test_healthcheck_reports_a_failed_job_next_to_a_newer_good_one(tmp_path):
    # Regression: the newest run of ANY job decided, so another job's good run
    # hid this job's failure from the container healthcheck.
    env = _two_jobs_env(tmp_path)
    dd = tmp_path / "data"
    _record(dd, "files", 5, "error", failed=["files"])
    _record(dd, "db", 1)
    out = runner.invoke(app, ["healthcheck"], env=env)
    assert out.exit_code == 1
    assert out.stdout.startswith("unhealthy: job files: the last backup failed")


def test_healthcheck_uses_the_jobs_own_max_age(tmp_path):
    env = _with_job(_env(tmp_path), healthcheck_max_age_hours=170)
    _record(tmp_path / "data", "main", 100)
    assert runner.invoke(app, ["healthcheck"], env=env).exit_code == 0
    assert runner.invoke(app, ["healthcheck"], env=_env_without_job_max_age(env)).exit_code == 1


def _env_without_job_max_age(env):
    cfg = json.loads(env["BACKUP_CONFIG_JSON"])
    cfg["jobs"][0].pop("healthcheck_max_age_hours")
    return {**env, "BACKUP_CONFIG_JSON": json.dumps(cfg)}


def test_healthcheck_still_judges_the_data_dir_when_the_config_does_not_load(tmp_path):
    env = {**_env(tmp_path), "BACKUP_CONFIG_JSON": "{not json"}
    _record(tmp_path / "data", "main", 1)
    out = runner.invoke(app, ["healthcheck"], env=env)
    assert out.exit_code == 0 and out.stdout.startswith("healthy: the last backup is fresh")
    assert "config not loaded" in out.stderr
