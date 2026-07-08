"""Hermetic CLI tests via Typer's CliRunner (filesystem source, local dest)."""

import json

from typer.testing import CliRunner

from backuphelper.cli import app

runner = CliRunner()


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


def test_download_copies_artifact(tmp_path):
    env = _env(tmp_path)
    runner.invoke(app, ["create"], env=env)
    sid = runner.invoke(app, ["list"], env=env).stdout.split()[0]
    out = tmp_path / "out"
    assert runner.invoke(app, ["download", sid, str(out)], env=env).exit_code == 0
    assert (out / f"{sid}.tar.gz").exists() and (out / f"{sid}.manifest.json").exists()
