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


def test_invalid_config_error_does_not_echo_the_value():
    # A JSON number where the SMTP password expects text fails validation; the
    # error (printed at startup) must name the field, never the value.
    env = {"BACKUP_CONFIG_JSON": json.dumps(
        {"jobs": [{"name": "main", "notifications": {"email": {"password": 20261006}}}]})}
    with pytest.raises(ConfigError) as info:
        load_config(env=env)
    assert "password" in str(info.value)
    assert "20261006" not in str(info.value)


# ── discrete overrides are typed by the field they set ───────────────────────
def _with(**overrides):
    base = {"jobs": [{"name": "main",
                      "sources": [{"type": "postgres", "host": "db", "database": "app"}],
                      "destinations": [{"type": "s3", "bucket": "offsite"}]}]}
    return {"BACKUP_CONFIG_JSON": json.dumps(base), **overrides}


def test_a_numeric_secret_override_stays_text():
    # Regression: the override was JSON-parsed into the int 20261006, so a
    # numeric SMTP password failed validation and the daemon did not start.
    cfg = load_config(env=_with(BACKUP_JOBS__0__NOTIFICATIONS__EMAIL__PASSWORD="20261006"))
    assert cfg.jobs[0].notifications.email.password == "20261006"


def test_a_numeric_source_or_destination_secret_override_stays_text():
    # A source's and a destination's own keys are not declared by the engine;
    # a number there used to arrive as an int and failed the source's model.
    from backuphelper.plugins.registry import build_source

    cfg = load_config(env=_with(BACKUP_JOBS__0__SOURCES__0__PASSWORD="20261006",
                                BACKUP_JOBS__0__SOURCES__0__USER="1000",
                                BACKUP_JOBS__0__DESTINATIONS__0__SECRET_KEY="90817263"))
    job = cfg.jobs[0]
    assert job.sources[0].model_extra["password"] == "20261006"
    assert job.destinations[0].model_extra["secret_key"] == "90817263"
    source = build_source(job.sources[0].model_dump())  # the source's own model accepts it
    assert source.cfg.password == "20261006" and source.cfg.user == "1000"


def test_overrides_never_reformat_numeric_looking_text():
    # "1e5" used to become 100000.0 and "1.50" became 1.5 - silently another
    # value for a text field or a key the engine does not declare.
    cfg = load_config(env=_with(BACKUP_JOBS__0__NAME="1.50",
                                BACKUP_JOBS__0__SCHEDULE__HOUR="3",
                                BACKUP_JOBS__0__SCHEDULE__MINUTE="05",
                                BACKUP_JOBS__0__SOURCES__0__LABEL="1e5",
                                BACKUP_JOBS__0__SOURCES__0__VERSION="1.50",
                                BACKUP_JOBS__0__ENCRYPTION__RECIPIENT="null"))
    job = cfg.jobs[0]
    assert job.name == "1.50"
    assert (job.schedule.hour, job.schedule.minute) == ("3", "05")
    assert job.sources[0].model_extra["label"] == "1e5"
    assert job.sources[0].model_extra["version"] == "1.50"
    assert job.encryption.recipient == "null"


def test_number_boolean_and_structured_overrides_keep_working():
    from backuphelper.destinations.s3 import S3DestinationConfig
    from backuphelper.plugins.registry import build_source

    cfg = load_config(env=_with(BACKUP_JOBS__0__RETENTION__COUNT="30",
                                BACKUP_JOBS__0__RETENTION__GFS='{"daily": 7}',
                                BACKUP_JOBS__0__KEEP_LOCAL="false",
                                BACKUP_JOBS__0__SCHEDULE__INTERVAL_HOURS="1e1",
                                BACKUP_JOBS__0__SCHEDULE__ON_STARTUP="true",
                                BACKUP_JOBS__0__NOTIFICATIONS__CHANNELS='["email"]',
                                BACKUP_JOBS__0__NOTIFICATIONS__EMAIL__PORT="465",
                                BACKUP_JOBS__0__SOURCES__0__PORT="5433",
                                BACKUP_JOBS__0__SOURCES__0__ENABLED="false",
                                BACKUP_JOBS__0__SOURCES__0__EXCLUDE_TABLE_DATA='["logs"]',
                                BACKUP_JOBS__0__DESTINATIONS__0__ENSURE_BUCKET="false",
                                BACKUP_JOBS__0__DESTINATIONS__0__MULTIPART_THRESHOLD="1048576"))
    job = cfg.jobs[0]
    assert job.retention.count == 30 and job.retention.gfs.daily == 7
    assert job.keep_local is False and job.schedule.on_startup is True
    assert job.schedule.interval_hours == 10
    assert job.notifications.channels == ["email"] and job.notifications.email.port == 465
    extra = job.sources[0].model_extra
    assert extra["enabled"] is False and extra["exclude_table_data"] == ["logs"]
    assert build_source(job.sources[0].model_dump()).cfg.port == 5433
    dest = S3DestinationConfig.model_validate(job.destinations[0].model_dump(exclude={"type"}))
    assert dest.ensure_bucket is False and dest.multipart_threshold == 1048576
