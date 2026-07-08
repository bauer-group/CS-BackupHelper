"""Filesystem source — one named path-group → deterministic tar.gz.

A job lists N filesystem sources for N independent path-groups (WordPress
uploads + content, ZAMMAD storage, NocoDB data). Each produces one component
``<name>.tar.gz`` and restores by overlaying its extracted tree onto ``path``.
"""

from __future__ import annotations

import fnmatch
import gzip
import shutil
import tarfile
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field, field_validator

from .base import Source, StagedComponent


class FilesystemConfig(BaseModel):
    name: str = "files"
    path: str
    subdirs: Optional[list[str]] = None  # if set, only these subdirs of path
    exclude: list[str] = Field(default_factory=list)  # fnmatch globs on rel path

    @field_validator("subdirs", "exclude", mode="before")
    @classmethod
    def _csv_or_list(cls, v: object, info) -> object:
        # Accept a comma-separated string as well as a list, so a compose-
        # interpolated CSV env (e.g. BACKUP_CONTENT_DIRS=plugins,themes,languages)
        # is a drop-in. An empty/whitespace CSV means "unset" (None for the
        # optional subdirs, [] for the always-a-list exclude).
        if isinstance(v, str):
            parts = [part.strip() for part in v.split(",") if part.strip()]
            if parts:
                return parts
            return None if info.field_name == "subdirs" else []
        return v


class FilesystemSource(Source):
    type = "filesystem"

    def __init__(self, spec: Mapping[str, Any]):
        super().__init__(spec)
        self.cfg = FilesystemConfig.model_validate(
            {k: v for k, v in spec.items() if k != "type"}
        )

    @property
    def component_name(self) -> str:
        return self.cfg.name

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        base = Path(self.cfg.path)
        out = staging_dir / f"{self.cfg.name}.tar.gz"
        if not base.exists():
            return [StagedComponent(name=self.cfg.name, kind=self.type, path=None,
                                    error=f"path not found: {base}")]
        members = self._collect(base)
        _write_deterministic_targz(members, out)
        return [StagedComponent(name=self.cfg.name, kind=self.type, path=out,
                                metadata={"path": str(base), "file_count": len(members)})]

    def restore(self, staged_dir: Path) -> None:
        """Overlay the extracted component tree onto the configured path."""
        target = Path(self.cfg.path)
        target.mkdir(parents=True, exist_ok=True)
        for item in Path(staged_dir).rglob("*"):
            if item.is_file():
                rel = item.relative_to(staged_dir)
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)

    def _collect(self, base: Path) -> list[tuple[str, Path]]:
        roots = [base / s for s in self.cfg.subdirs] if self.cfg.subdirs else [base]
        members: list[tuple[str, Path]] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                arcname = path.relative_to(base).as_posix()
                if self._excluded(arcname):
                    continue
                members.append((arcname, path))
        members.sort(key=lambda m: m[0])
        return members

    def _excluded(self, arcname: str) -> bool:
        return any(fnmatch.fnmatch(arcname, pat) for pat in self.cfg.exclude)


def _write_deterministic_targz(members: list[tuple[str, Path]], out: Path) -> None:
    """Write a byte-deterministic tar.gz: sorted members, mtime=0, no gzip name."""
    with open(out, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w:") as tar:
                for arcname, path in members:
                    info = tar.gettarinfo(str(path), arcname=arcname)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with open(path, "rb") as fh:
                        tar.addfile(info, fh)
