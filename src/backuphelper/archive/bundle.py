"""Deterministic tar.gz bundling and path-traversal-safe extraction."""

from __future__ import annotations

import gzip
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath


def create_bundle(staging_dir: Path, archive_path: Path) -> None:
    """Bundle the contents of ``staging_dir`` into a deterministic tar.gz.

    Byte-determinism for identical input trees is achieved by adding members
    in sorted path order, zeroing every TarInfo's mtime/uid/gid/uname/gname,
    and forcing the GZIP header mtime to 0.
    """
    staging_dir = Path(staging_dir)
    archive_path = Path(archive_path)

    members = sorted(
        staging_dir.rglob("*"),
        key=lambda p: p.relative_to(staging_dir).as_posix(),
    )

    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w:") as tar:
                for path in members:
                    arcname = path.relative_to(staging_dir).as_posix()
                    info = tar.gettarinfo(str(path), arcname=arcname)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    if path.is_file():
                        with path.open("rb") as fh:
                            tar.addfile(info, fh)
                    else:
                        tar.addfile(info)


def _is_safe_member(name: str) -> bool:
    """Reject absolute paths and any '..' component (path traversal)."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or PureWindowsPath(name).is_absolute():
        return False
    return ".." not in pure.parts


def extract_bundle(archive_path: Path, dest_dir: Path) -> Path:
    """Safely extract ``archive_path`` into ``dest_dir`` and return ``dest_dir``.

    Members whose name is absolute or contains a ``..`` component are skipped
    to prevent path traversal outside ``dest_dir``.
    """
    archive_path = Path(archive_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, mode="r:gz") as tar:
        safe = [m for m in tar.getmembers() if _is_safe_member(m.name)]
        tar.extractall(path=dest_dir, members=safe, filter="data")

    return dest_dir
