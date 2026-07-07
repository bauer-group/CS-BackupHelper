"""Tests for the filesystem source (named path-group, deterministic tar)."""

import gzip
import io
import tarfile
from pathlib import Path

from backuphelper.sources.filesystem import FilesystemSource


def _tree(base: Path):
    (base / "a.txt").write_text("A")
    (base / "sub").mkdir()
    (base / "sub" / "b.txt").write_text("B")
    (base / "cache").mkdir()
    (base / "cache" / "junk.tmp").write_text("junk")


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return sorted(tar.getnames())


def test_produce_creates_named_targz_with_files(tmp_path):
    src_dir = tmp_path / "uploads"
    src_dir.mkdir()
    _tree(src_dir)
    staging = tmp_path / "stage"
    src = FilesystemSource({"type": "filesystem", "name": "uploads", "path": str(src_dir)})
    comps = src.produce(staging)
    assert len(comps) == 1
    c = comps[0]
    assert c.name == "uploads" and c.kind == "filesystem" and c.error is None
    assert c.path == staging / "uploads.tar.gz"
    names = _members(c.path)
    assert "a.txt" in names and "sub/b.txt" in names


def test_exclude_pattern_skips_matching_files(tmp_path):
    src_dir = tmp_path / "uploads"
    src_dir.mkdir()
    _tree(src_dir)
    src = FilesystemSource(
        {"type": "filesystem", "name": "uploads", "path": str(src_dir), "exclude": ["cache/*"]}
    )
    c = src.produce(tmp_path / "stage")[0]
    names = _members(c.path)
    assert not any(n.startswith("cache/") for n in names)
    assert "a.txt" in names


def test_subdirs_limits_included_paths(tmp_path):
    base = tmp_path / "wp-content"
    base.mkdir()
    (base / "plugins").mkdir()
    (base / "plugins" / "p.php").write_text("x")
    (base / "uploads").mkdir()
    (base / "uploads" / "img.jpg").write_text("y")
    src = FilesystemSource(
        {"type": "filesystem", "name": "content", "path": str(base), "subdirs": ["plugins"]}
    )
    names = _members(src.produce(tmp_path / "stage")[0].path)
    assert any(n.startswith("plugins/") for n in names)
    assert not any(n.startswith("uploads/") for n in names)


def test_produce_is_byte_deterministic(tmp_path):
    src_dir = tmp_path / "uploads"
    src_dir.mkdir()
    _tree(src_dir)
    a = FilesystemSource({"type": "filesystem", "name": "u", "path": str(src_dir)}).produce(tmp_path / "s1")[0]
    b = FilesystemSource({"type": "filesystem", "name": "u", "path": str(src_dir)}).produce(tmp_path / "s2")[0]
    assert a.path.read_bytes() == b.path.read_bytes()


def test_gzip_header_mtime_is_zero(tmp_path):
    src_dir = tmp_path / "u"
    src_dir.mkdir()
    (src_dir / "a").write_text("a")
    c = FilesystemSource({"type": "filesystem", "name": "u", "path": str(src_dir)}).produce(tmp_path / "s")[0]
    raw = c.path.read_bytes()
    assert int.from_bytes(raw[4:8], "little") == 0  # gzip MTIME field


def test_restore_overlays_files_into_target(tmp_path):
    # Build a component dir (as the engine would after extraction) and restore it.
    staged = tmp_path / "extracted"
    (staged / "sub").mkdir(parents=True)
    (staged / "a.txt").write_text("A")
    (staged / "sub" / "b.txt").write_text("B")
    target = tmp_path / "restored"
    FilesystemSource({"type": "filesystem", "name": "u", "path": str(target)}).restore(staged)
    assert (target / "a.txt").read_text() == "A"
    assert (target / "sub" / "b.txt").read_text() == "B"


def test_missing_path_produces_errored_component(tmp_path):
    src = FilesystemSource({"type": "filesystem", "name": "u", "path": str(tmp_path / "nope")})
    c = src.produce(tmp_path / "stage")[0]
    assert c.error is not None and c.path is None
