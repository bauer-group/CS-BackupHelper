"""Local filesystem destination — artifacts stored under ``root/<key>``.

Keys map directly onto a directory tree beneath ``root``. Parent directories are
created on write; :meth:`list_keys` returns keys relative to ``root`` in posix
form, sorted, so ordering is stable across platforms.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .base import Destination

logger = logging.getLogger(__name__)


class LocalDestination(Destination):
    """A :class:`Destination` backed by a local directory tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, local_path: Path, key: str) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)
        logger.debug("stored key %s", key)

    def get(self, key: str, dest: Path) -> None:
        source = self._path(key)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)

    def list_keys(self, prefix: str = "") -> list[str]:
        if not self.root.exists():
            return []
        keys = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        ]
        return sorted(k for k in keys if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
        logger.debug("deleted key %s", key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
