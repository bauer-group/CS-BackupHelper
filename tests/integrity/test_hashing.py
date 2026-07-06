"""Tests for streamed sha256 hashing (constant memory, multi-GB safe)."""

import hashlib

from backuphelper.integrity.hashing import sha256_file


def test_matches_hashlib_for_known_content(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    assert sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


def test_empty_file_hashes_to_the_empty_digest(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert sha256_file(p) == hashlib.sha256(b"").hexdigest()


def test_streams_content_larger_than_one_chunk(tmp_path):
    data = b"x" * (1024 * 1024 * 2 + 7)  # > 2 chunks of 1 MiB
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    assert sha256_file(p) == hashlib.sha256(data).hexdigest()
