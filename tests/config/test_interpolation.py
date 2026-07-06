"""Tests for ${VAR} interpolation and _FILE secret indirection in config trees."""

import pytest

from backuphelper.config.interpolation import interpolate, MissingEnvVar


def test_replaces_a_simple_placeholder_from_injected_env():
    env = {"DB_PASSWORD": "s3cret"}
    assert interpolate("${DB_PASSWORD}", env) == "s3cret"


def test_replaces_placeholder_embedded_in_a_larger_string():
    env = {"HOST": "db.internal"}
    assert interpolate("postgres://${HOST}:5432", env) == "postgres://db.internal:5432"


def test_recurses_into_dicts_and_lists():
    env = {"TOKEN": "abc", "BUCKET": "backups"}
    tree = {
        "auth": {"token": "${TOKEN}"},
        "targets": [{"bucket": "${BUCKET}"}, "static"],
        "count": 14,
    }
    assert interpolate(tree, env) == {
        "auth": {"token": "abc"},
        "targets": [{"bucket": "backups"}, "static"],
        "count": 14,
    }


def test_leaves_non_placeholder_values_untouched():
    assert interpolate({"n": 3, "flag": True, "s": "plain"}, {}) == {
        "n": 3,
        "flag": True,
        "s": "plain",
    }


def test_missing_variable_raises_with_the_variable_name():
    with pytest.raises(MissingEnvVar, match="NOPE"):
        interpolate("${NOPE}", {})
