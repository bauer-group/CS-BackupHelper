"""Tests for the whitelist env-snapshot source."""

import json

from backuphelper.sources.env_snapshot import EnvSnapshotSource


def test_captures_only_whitelisted_vars(tmp_path):
    environ = {"APP_URL": "https://x", "SECRET_TInY": "s", "PATH": "/bin"}
    src = EnvSnapshotSource({"type": "env", "whitelist": ["APP_URL"]}, environ=environ)
    c = src.produce(tmp_path)[0]
    assert c.kind == "env" and c.error is None
    data = json.loads(c.path.read_text())
    assert data == {"APP_URL": "https://x"}


def test_glob_pattern_matches_multiple_vars(tmp_path):
    environ = {"APP_A": "1", "APP_B": "2", "OTHER": "3"}
    src = EnvSnapshotSource({"type": "env", "whitelist": ["APP_*"]}, environ=environ)
    data = json.loads(src.produce(tmp_path)[0].path.read_text())
    assert data == {"APP_A": "1", "APP_B": "2"}


def test_keys_are_sorted_for_determinism(tmp_path):
    environ = {"Z": "1", "A": "2", "M": "3"}
    src = EnvSnapshotSource({"type": "env", "whitelist": ["*"]}, environ=environ)
    text = src.produce(tmp_path)[0].path.read_text()
    assert list(json.loads(text).keys()) == ["A", "M", "Z"]


def test_empty_whitelist_captures_nothing(tmp_path):
    src = EnvSnapshotSource({"type": "env", "whitelist": []}, environ={"A": "1"})
    assert json.loads(src.produce(tmp_path)[0].path.read_text()) == {}
