"""Tests for source plugin discovery/registration."""

import pytest

from backuphelper.plugins.registry import (
    SourceNotFound,
    build_source,
    get_source_class,
)
from backuphelper.sources.base import Source
from backuphelper.sources.filesystem import FilesystemSource
from backuphelper.sources.postgres import PostgresSource


def test_builtin_types_resolve():
    assert get_source_class("postgres") is PostgresSource
    assert get_source_class("filesystem") is FilesystemSource


def test_unknown_type_raises():
    with pytest.raises(SourceNotFound, match="nope"):
        get_source_class("nope")


def test_build_source_instantiates_from_spec(tmp_path):
    src = build_source({"type": "env", "whitelist": []})
    assert src.type == "env"


def test_plugin_entry_points_are_consulted():
    class CustomSource(Source):
        type = "custom"

        def produce(self, staging_dir):
            return []

    # Injected plugin loader simulates a repo-registered entry point.
    cls = get_source_class("custom", load_plugins=lambda: {"custom": CustomSource})
    assert cls is CustomSource


def test_builtins_take_precedence_is_not_shadowed_by_plugin_lookup():
    # A plugin loader that would also define postgres must not break builtin resolution.
    cls = get_source_class("postgres", load_plugins=lambda: {"custom": PostgresSource})
    assert cls is PostgresSource
