"""The Source extension contract.

A ``Source`` knows how to dump one backend into a staging directory and return
the ``StagedComponent``s it produced. The engine (not the source) hashes the
staged files, bundles them, applies retention and uploads — so a source only
has to answer WHAT to capture, never HOW to move bytes. ``restore`` is optional;
DB/filesystem sources implement it, exotic ones may not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional


@dataclass
class StagedComponent:
    """One artifact a source staged on disk (or failed to)."""

    name: str
    kind: str
    path: Optional[Path]
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None


class SourceError(Exception):
    """A source failed to produce or restore its data."""


class Source(ABC):
    """Base class for all sources. ``type`` is the config discriminator."""

    type: ClassVar[str] = ""

    def __init__(self, spec: Mapping[str, Any]):
        self.spec = dict(spec)

    @abstractmethod
    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        """Dump into ``staging_dir`` and return the staged components."""

    def restore(self, staged_dir: Path) -> None:
        """Restore from a previously staged/extracted component directory."""
        raise NotImplementedError(f"{self.type} source does not support restore")
