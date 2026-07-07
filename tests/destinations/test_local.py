"""Tests for the local filesystem destination."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuphelper.destinations.local import LocalDestination


def test_put_exists_get_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "artifact.bin"
    src.write_bytes(b"payload-123")
    dest = LocalDestination(tmp_path / "store")

    assert dest.exists("2026/artifact.bin") is False
    dest.put(src, "2026/artifact.bin")
    assert dest.exists("2026/artifact.bin") is True

    out = tmp_path / "restored.bin"
    dest.get("2026/artifact.bin", out)
    assert out.read_bytes() == b"payload-123"


def test_list_keys_sorted_with_prefix_filter(tmp_path: Path) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    dest = LocalDestination(tmp_path / "store")
    for key in ("b/2.bin", "a/1.bin", "b/1.bin", "c/9.bin"):
        dest.put(src, key)

    assert dest.list_keys() == ["a/1.bin", "b/1.bin", "b/2.bin", "c/9.bin"]
    assert dest.list_keys("b/") == ["b/1.bin", "b/2.bin"]


def test_delete_removes_key(tmp_path: Path) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    dest = LocalDestination(tmp_path / "store")
    dest.put(src, "some/thing.bin")
    assert dest.exists("some/thing.bin") is True

    dest.delete("some/thing.bin")
    assert dest.exists("some/thing.bin") is False


def test_get_of_missing_key_raises_file_not_found(tmp_path: Path) -> None:
    dest = LocalDestination(tmp_path / "store")
    with pytest.raises(FileNotFoundError):
        dest.get("nope.bin", tmp_path / "out.bin")
