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


def test_healthcheck_grace_when_no_backup(tmp_path):
    env = {"BACKUP_DATA_DIR": str(tmp_path / "empty")}
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
