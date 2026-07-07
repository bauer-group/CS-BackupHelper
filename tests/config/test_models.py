"""Tests for the config model hierarchy (RootConfig / Job / specs)."""

import pytest
from pydantic import ValidationError

from backuphelper.config.models import (
    DestinationSpec,
    Job,
    RetentionConfig,
    RootConfig,
    SourceSpec,
)


def test_minimal_root_config_applies_defaults():
    cfg = RootConfig.model_validate({"instance_name": "iam", "jobs": [{"name": "main"}]})
    assert cfg.version == 1
    assert cfg.instance_name == "iam"
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "main"


def test_job_defaults_to_local_destination():
    job = Job.model_validate({"name": "main"})
    assert [d.type for d in job.destinations] == ["local"]


def test_source_spec_requires_type_and_keeps_extra_fields():
    # Open for plugin source types: unknown fields are preserved, not dropped.
    spec = SourceSpec.model_validate({"type": "postgres", "host": "db", "db": "logto"})
    assert spec.type == "postgres"
    assert spec.model_extra == {"host": "db", "db": "logto"}


def test_source_spec_without_type_is_rejected():
    with pytest.raises(ValidationError):
        SourceSpec.model_validate({"host": "db"})


def test_destination_type_is_restricted_to_local_or_s3():
    DestinationSpec.model_validate({"type": "s3", "bucket": "b"})
    DestinationSpec.model_validate({"type": "local"})
    with pytest.raises(ValidationError):
        DestinationSpec.model_validate({"type": "ftp"})


def test_notify_channels_accept_comma_string():
    from backuphelper.config.models import NotifyConfig
    assert NotifyConfig(channels="email, webhook ,teams").channels == ["email", "webhook", "teams"]
    assert NotifyConfig(channels="").channels == []
    assert NotifyConfig(channels=["email"]).channels == ["email"]  # list still works


def test_retention_defaults():
    r = RetentionConfig()
    assert r.count == 14
    assert r.smart_last is True
    assert r.age_days == 0
