"""Tests for the S3 destination — path-style SigV4 with hand-rolled multipart."""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

import boto3
from moto import mock_aws

from backuphelper.destinations.s3 import S3Destination

REGION = "eu-central-1"

# moto (like real S3) rejects non-final multipart parts smaller than 5 MiB with
# EntityTooSmall, so the multipart test uses a 5 MiB chunk and an 11 MiB file.
CHUNK = 5 * 1024 * 1024


class _SpyClient:
    """Wraps a boto3 client and counts method invocations by name."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: collections.Counter = collections.Counter()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return attr(*args, **kwargs)

        return wrapper


def _cfg(bucket: str, **overrides: object) -> dict:
    cfg: dict = {
        "bucket": bucket,
        "region": REGION,
        "access_key": "test",
        "secret_key": "test",
    }
    cfg.update(overrides)
    return cfg


def _client():
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _create_bucket(name: str) -> None:
    _client().create_bucket(
        Bucket=name,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )


@mock_aws
def test_small_file_put_exists_get_roundtrip(tmp_path: Path) -> None:
    _create_bucket("backups")
    dest = S3Destination(_cfg("backups"))

    src = tmp_path / "a.bin"
    src.write_bytes(b"hello world")

    assert dest.exists("2026/a.bin") is False
    dest.put(src, "2026/a.bin")
    assert dest.exists("2026/a.bin") is True

    out = tmp_path / "out.bin"
    dest.get("2026/a.bin", out)
    assert out.read_bytes() == b"hello world"


@mock_aws
def test_small_put_verifies_size_via_head(tmp_path: Path) -> None:
    # A single put_object (< multipart_threshold) must head-verify its size like
    # the multipart path does, so a truncated upload is caught immediately.
    _create_bucket("backups")
    dest = S3Destination(_cfg("backups"))
    spy = _SpyClient(dest._client)
    dest._client = spy
    src = tmp_path / "s.bin"
    src.write_bytes(b"hello")
    dest.put(src, "k/s.bin")
    assert spy.calls["put_object"] >= 1
    assert spy.calls["head_object"] >= 1, "single put must verify size via head_object"


@mock_aws
def test_single_put_raises_on_size_mismatch(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    import pytest

    _create_bucket("backups")
    dest = S3Destination(_cfg("backups"))
    fake = MagicMock()
    fake.put_object.return_value = {}
    fake.head_object.return_value = {"ContentLength": 999}  # wrong size
    dest._client = fake
    src = tmp_path / "s.bin"
    src.write_bytes(b"hello")  # 5 bytes
    with pytest.raises(RuntimeError, match="size mismatch"):
        dest.put(src, "k/s.bin")


@mock_aws
def test_get_streams_to_disk_not_into_memory(tmp_path: Path) -> None:
    # get() must stream the object to disk (download_file) rather than buffering
    # the whole archive in RAM via get_object()["Body"].read() — a multi-GB
    # snapshot (e.g. DocumentSigning's PDF-bucket mirror) would OOM otherwise.
    _create_bucket("backups")
    dest = S3Destination(_cfg("backups"))
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * (2 * 1024 * 1024))
    dest.put(src, "s/big.bin")

    spy = _SpyClient(dest._client)
    dest._client = spy
    out = tmp_path / "out.bin"
    dest.get("s/big.bin", out)

    assert out.read_bytes() == src.read_bytes()
    assert spy.calls["get_object"] == 0, "get() must not buffer the whole object via get_object().read()"
    assert spy.calls["download_file"] >= 1


@mock_aws
def test_list_keys_sorted_and_strips_config_prefix(tmp_path: Path) -> None:
    _create_bucket("backups")
    dest = S3Destination(_cfg("backups", prefix="team/"))
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    for key in ("logs/b.txt", "logs/a.txt", "data/1.bin"):
        dest.put(src, key)

    # Config prefix is prepended on write and stripped on list.
    raw = {o["Key"] for o in _client().list_objects_v2(Bucket="backups")["Contents"]}
    assert raw == {"team/logs/b.txt", "team/logs/a.txt", "team/data/1.bin"}

    assert dest.list_keys() == ["data/1.bin", "logs/a.txt", "logs/b.txt"]
    assert dest.list_keys("logs/") == ["logs/a.txt", "logs/b.txt"]


@mock_aws
def test_delete_removes_key(tmp_path: Path) -> None:
    _create_bucket("backups")
    dest = S3Destination(_cfg("backups", prefix="p/"))
    src = tmp_path / "f.bin"
    src.write_bytes(b"gone")
    dest.put(src, "x/y.bin")
    assert dest.exists("x/y.bin") is True

    dest.delete("x/y.bin")
    assert dest.exists("x/y.bin") is False


@mock_aws
def test_multipart_upload_roundtrip_and_size_verification(tmp_path: Path) -> None:
    _create_bucket("backups")
    spy = _SpyClient(_client())
    dest = S3Destination(
        _cfg("backups", multipart_threshold=CHUNK, multipart_chunk_size=CHUNK),
        client=spy,
    )

    payload = b"A" * (11 * 1024 * 1024)  # 11 MiB -> 5 + 5 + 1 = 3 equal chunks
    src = tmp_path / "big.bin"
    src.write_bytes(payload)

    dest.put(src, "archives/big.bin")

    # The hand-rolled multipart path — NOT a single put_object — was taken.
    assert spy.calls["create_multipart_upload"] == 1
    assert spy.calls["upload_part"] == 3
    assert spy.calls["complete_multipart_upload"] == 1
    assert spy.calls["put_object"] == 0
    # head_object size verification ran and passed (no abort).
    assert spy.calls["head_object"] >= 1
    assert spy.calls["abort_multipart_upload"] == 0

    out = tmp_path / "out.bin"
    dest.get("archives/big.bin", out)
    assert out.read_bytes() == payload


@mock_aws
def test_ensure_bucket_creates_missing_bucket(tmp_path: Path) -> None:
    # Bucket does NOT exist yet; construction with ensure_bucket=True creates it.
    existing = _client().list_buckets()["Buckets"]
    assert all(b["Name"] != "fresh" for b in existing)

    dest = S3Destination(_cfg("fresh", ensure_bucket=True))

    names = {b["Name"] for b in _client().list_buckets()["Buckets"]}
    assert "fresh" in names

    # And the freshly created bucket is usable.
    src = tmp_path / "f.bin"
    src.write_bytes(b"created")
    dest.put(src, "k.bin")
    assert dest.exists("k.bin") is True
