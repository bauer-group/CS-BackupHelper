"""Tests for the Source extension contract."""

from pathlib import Path

import pytest

from backuphelper.sources.base import Source, StagedComponent


def test_staged_component_defaults():
    sc = StagedComponent(name="database", kind="postgres", path=Path("/x/database.dump"))
    assert sc.metadata == {}
    assert sc.error is None


def test_staged_component_can_carry_an_error_without_a_path():
    sc = StagedComponent(name="creds", kind="n8n", path=None, error="export failed")
    assert sc.path is None
    assert sc.error == "export failed"


def test_source_restore_defaults_to_not_implemented():
    class Dummy(Source):
        type = "dummy"

        def produce(self, staging_dir):  # pragma: no cover - not exercised here
            return []

    with pytest.raises(NotImplementedError):
        Dummy({}).restore(Path("/tmp"))


def test_source_stores_its_spec():
    class Dummy(Source):
        type = "dummy"

        def produce(self, staging_dir):
            return []

    d = Dummy({"host": "db", "db": "logto"})
    assert d.spec["host"] == "db"
