"""Tests for deterministic tar.gz bundling and safe extraction."""

import io
import struct
import tarfile
from pathlib import Path

from backuphelper.archive.bundle import create_bundle, extract_bundle


def _make_tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("bravo", encoding="utf-8")


def test_identical_tree_produces_byte_identical_archive(tmp_path):
    tree1 = tmp_path / "t1"
    tree2 = tmp_path / "t2"
    _make_tree(tree1)
    _make_tree(tree2)

    arc1 = tmp_path / "a1.tar.gz"
    arc2 = tmp_path / "a2.tar.gz"
    create_bundle(tree1, arc1)
    create_bundle(tree2, arc2)

    assert arc1.read_bytes() == arc2.read_bytes()


def test_members_are_stored_in_sorted_order(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    for name in ["zebra.txt", "apple.txt", "mango.txt"]:
        (tree / name).write_text(name, encoding="utf-8")

    arc = tmp_path / "a.tar.gz"
    create_bundle(tree, arc)

    with tarfile.open(arc, mode="r:gz") as tar:
        names = tar.getnames()
    assert names == sorted(names)
    assert names == ["apple.txt", "mango.txt", "zebra.txt"]


def test_gzip_header_mtime_is_zero(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "a.txt").write_text("alpha", encoding="utf-8")

    arc = tmp_path / "a.tar.gz"
    create_bundle(tree, arc)

    header = arc.read_bytes()[:10]
    assert header[:2] == b"\x1f\x8b"  # gzip magic
    (mtime,) = struct.unpack("<I", header[4:8])  # little-endian mtime field
    assert mtime == 0


def test_roundtrip_reproduces_contents_and_layout(tmp_path):
    tree = tmp_path / "t"
    _make_tree(tree)

    arc = tmp_path / "a.tar.gz"
    create_bundle(tree, arc)

    dest = tmp_path / "out"
    returned = extract_bundle(arc, dest)

    assert returned == dest
    assert (dest / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (dest / "sub" / "b.txt").read_text(encoding="utf-8") == "bravo"


def _add_bytes(tar, name, data):
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def test_extraction_skips_path_traversal_member(tmp_path):
    arc = tmp_path / "evil.tar.gz"
    with tarfile.open(arc, mode="w:gz") as tar:
        _add_bytes(tar, "../evil.txt", b"pwned")
        _add_bytes(tar, "good.txt", b"safe")

    dest = tmp_path / "out"
    extract_bundle(arc, dest)

    # The traversal member must not be written outside (or inside) dest.
    assert not (tmp_path / "evil.txt").exists()
    assert not (dest / "evil.txt").exists()
    assert not (dest.parent / "evil.txt").exists()
    # The legit member is still extracted.
    assert (dest / "good.txt").read_bytes() == b"safe"


def test_extraction_skips_absolute_path_member(tmp_path):
    arc = tmp_path / "abs.tar.gz"
    with tarfile.open(arc, mode="w:gz") as tar:
        _add_bytes(tar, "/tmp/abs_evil.txt", b"pwned")
        _add_bytes(tar, "ok.txt", b"safe")

    dest = tmp_path / "out"
    extract_bundle(arc, dest)

    assert not (dest / "tmp" / "abs_evil.txt").exists()
    assert not (dest / "abs_evil.txt").exists()
    assert (dest / "ok.txt").read_bytes() == b"safe"
