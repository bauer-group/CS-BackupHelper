"""The Destination extension contract.

A ``Destination`` is an object store of backup artifacts keyed by string keys.
The engine hands it a local file and a key; the destination is responsible for
moving the bytes there (and back) — whether that is a local directory, an S3
bucket or any S3-compatible endpoint. Implementations must be prefix-aware and
key ordering from :meth:`list_keys` is always sorted for deterministic output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Destination(ABC):
    """Base class for all destinations — a keyed object store of artifacts."""

    @abstractmethod
    def put(self, local_path: Path, key: str) -> None:
        """Upload/copy the file at ``local_path`` to ``key``."""

    @abstractmethod
    def get(self, key: str, dest: Path) -> None:
        """Download/copy ``key`` into the local file ``dest``."""

    @abstractmethod
    def list_keys(self, prefix: str = "") -> list[str]:
        """Return all keys under ``prefix``, sorted."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove ``key`` from the store."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether ``key`` is present in the store."""
