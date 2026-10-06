"""Tests for the filesystem source (named path-group, deterministic tar)."""

import gzip
import io
import tarfile
from pathlib import Path

from backuphelper.sources.filesystem import FilesystemConfig, FilesystemSource


def test_subdirs_accepts_a_csv_string():
    # BACKUP_CONFIG_JSON often carries the subdir set as a compose-interpolated CSV
    # (e.g. WordPress' BACKUP_CONTENT_DIRS=plugins,themes,languages). Accept both a
    # CSV string and a JSON list, so the config stays a drop-in for the old var.
    assert FilesystemConfig(path="/x", subdirs="plugins, themes ,languages").subdirs == [
        "plugins", "themes", "languages"]
    assert FilesystemConfig(path="/x", subdirs=["a", "b"]).subdirs == ["a", "b"]
    assert FilesystemConfig(path="/x", subdirs="").subdirs is None      # empty CSV -> unset (all of path)
    assert FilesystemConfig(path="/x").subdirs is None


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


def _deny_listing(monkeypatch, *denied: Path):
    # chmod cannot simulate this: the test stage runs as root, which reads
    # everything. Make os.scandir refuse the given directories instead.
    import os

    real_scandir = os.scandir
    blocked = {str(p) for p in denied}

    def scandir(path="."):
        if str(path) in blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_unreadable_subdirectory_is_skipped_and_reported(tmp_path, monkeypatch):
    # Regression: Path.rglob swallowed the PermissionError silently. The readable
    # rest must still be backed up, and the gap reported in the metadata.
    src_dir = tmp_path / "files"
    src_dir.mkdir()
    _tree(src_dir)
    _deny_listing(monkeypatch, src_dir / "sub")
    src = FilesystemSource({"type": "filesystem", "name": "files", "path": str(src_dir)})
    staged = src.produce(tmp_path / "stage")[0]
    assert staged.error is None
    assert _members(staged.path) == ["a.txt", "cache/junk.tmp"]
    assert staged.metadata["file_count"] == 2
    assert staged.metadata["warnings"] == ["sub/ (directory not readable: Permission denied)"]


def test_unreadable_subdirs_root_is_skipped_and_the_other_subdirs_kept(tmp_path, monkeypatch):
    # With `subdirs` every subdir is a walk root; one unreadable root (e.g.
    # wp-content/plugins 0750) must not drop the readable siblings.
    src_dir = tmp_path / "wp-content"
    for sub in ("plugins", "themes", "languages"):
        (src_dir / sub).mkdir(parents=True)
        (src_dir / sub / f"{sub}.txt").write_text(sub)
    _deny_listing(monkeypatch, src_dir / "plugins")
    staged = FilesystemSource({"type": "filesystem", "name": "content", "path": str(src_dir),
                               "subdirs": "plugins,themes,languages"}).produce(tmp_path / "stage")[0]
    assert staged.error is None
    assert _members(staged.path) == ["languages/languages.txt", "themes/themes.txt"]
    assert staged.metadata["warnings"] == ["plugins/ (directory not readable: Permission denied)"]


def test_unreadable_lost_and_found_is_skipped_without_a_warning(tmp_path, monkeypatch):
    src_dir = tmp_path / "files"
    src_dir.mkdir()
    _tree(src_dir)
    (src_dir / "lost+found").mkdir()
    _deny_listing(monkeypatch, src_dir / "lost+found")
    staged = FilesystemSource({"type": "filesystem", "name": "files",
                               "path": str(src_dir)}).produce(tmp_path / "stage")[0]
    assert staged.error is None and "warnings" not in staged.metadata
    assert _members(staged.path) == ["a.txt", "cache/junk.tmp", "sub/b.txt"]


def test_unreadable_file_is_skipped_and_reported(tmp_path, monkeypatch):
    import backuphelper.sources.filesystem as fs

    src_dir = tmp_path / "files"
    src_dir.mkdir()
    _tree(src_dir)
    locked = src_dir / "sub" / "b.txt"

    def fake_open(path, *args, **kwargs):
        if str(path) == str(locked):
            raise PermissionError(13, "Permission denied", str(path))
        return open(path, *args, **kwargs)

    monkeypatch.setattr(fs, "open", fake_open, raising=False)
    staged = FilesystemSource({"type": "filesystem", "name": "files",
                               "path": str(src_dir)}).produce(tmp_path / "stage")[0]
    assert staged.error is None
    assert _members(staged.path) == ["a.txt", "cache/junk.tmp"]
    assert staged.metadata["file_count"] == 2
    assert staged.metadata["warnings"] == ["sub/b.txt (file not readable: Permission denied)"]


def test_many_unreadable_entries_are_capped_in_the_metadata(tmp_path, monkeypatch):
    src_dir = tmp_path / "files"
    src_dir.mkdir()
    dirs = []
    for i in range(25):
        d = src_dir / f"d{i:02d}"
        d.mkdir()
        dirs.append(d)
    _deny_listing(monkeypatch, *dirs)
    staged = FilesystemSource({"type": "filesystem", "name": "files",
                               "path": str(src_dir)}).produce(tmp_path / "stage")[0]
    warnings = staged.metadata["warnings"]
    assert len(warnings) == 21 and warnings[-1] == "... and 5 more"


def test_unreadable_root_fails_the_source(tmp_path, monkeypatch):
    import pytest

    src_dir = tmp_path / "files"
    src_dir.mkdir()
    _tree(src_dir)
    _deny_listing(monkeypatch, src_dir)
    src = FilesystemSource({"type": "filesystem", "name": "files", "path": str(src_dir)})
    with pytest.raises(PermissionError):
        src.produce(tmp_path / "stage")


def test_a_dir_star_exclude_skips_an_unreadable_directory(tmp_path, monkeypatch):
    src_dir = tmp_path / "files"
    src_dir.mkdir()
    _tree(src_dir)
    _deny_listing(monkeypatch, src_dir / "sub")
    src = FilesystemSource({"type": "filesystem", "name": "files", "path": str(src_dir),
                            "exclude": ["sub/*"]})
    staged = src.produce(tmp_path / "stage")
    assert _members(staged[0].path) == ["a.txt", "cache/junk.tmp"]
    assert "warnings" not in staged[0].metadata  # excluded on purpose: no warning
