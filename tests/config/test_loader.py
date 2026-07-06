"""Tests for the layered config loader (inline JSON / base64 / file / overrides)."""

import base64
import json

import pytest

from backuphelper.config.loader import ConfigError, load_config


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_no_config_returns_defaults():
    cfg = load_config(env={})
    assert cfg.version == 1
    assert cfg.jobs == []


def test_loads_from_inline_json_env():
    env = {"BACKUP_CONFIG_JSON": json.dumps({"instance_name": "iam", "jobs": [{"name": "main"}]})}
    cfg = load_config(env=env)
    assert cfg.instance_name == "iam"
    assert cfg.jobs[0].name == "main"


def test_loads_from_base64_json_env():
    env = {"BACKUP_CONFIG_JSON_BASE64": _b64({"instance_name": "b64", "jobs": []})}
    cfg = load_config(env=env)
    assert cfg.instance_name == "b64"


def test_loads_from_json_file(tmp_path):
    p = tmp_path / "backup.json"
    p.write_text(json.dumps({"instance_name": "fromfile", "jobs": []}))
    cfg = load_config(env={"BACKUP_CONFIG_FILE": str(p)})
    assert cfg.instance_name == "fromfile"


def test_loads_from_yaml_file(tmp_path):
    p = tmp_path / "backup.yaml"
    p.write_text("instance_name: yaml\njobs: []\n")
    cfg = load_config(env={"BACKUP_CONFIG_FILE": str(p)})
    assert cfg.instance_name == "yaml"


def test_inline_json_takes_precedence_over_file(tmp_path):
    p = tmp_path / "backup.json"
    p.write_text(json.dumps({"instance_name": "file"}))
    env = {
        "BACKUP_CONFIG_FILE": str(p),
        "BACKUP_CONFIG_JSON": json.dumps({"instance_name": "inline"}),
    }
    assert load_config(env=env).instance_name == "inline"


def test_interpolates_secret_placeholders_from_env():
    env = {
        "BACKUP_CONFIG_JSON": json.dumps(
            {"jobs": [{"name": "j", "sources": [{"type": "postgres", "password": "${DB_PW}"}]}]}
        ),
        "DB_PW": "topsecret",
    }
    cfg = load_config(env=env)
    assert cfg.jobs[0].sources[0].model_extra["password"] == "topsecret"


def test_discrete_env_override_beats_inline_json():
    env = {
        "BACKUP_CONFIG_JSON": json.dumps({"jobs": [{"name": "j", "retention": {"count": 5}}]}),
        "BACKUP_JOBS__0__RETENTION__COUNT": "30",
    }
    cfg = load_config(env=env)
    assert cfg.jobs[0].retention.count == 30


def test_invalid_json_raises_config_error():
    with pytest.raises(ConfigError):
        load_config(env={"BACKUP_CONFIG_JSON": "{not json"})
