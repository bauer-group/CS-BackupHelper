"""Schema-versioned, self-describing backup manifest.

A ``Component`` is the unit every Source produces (name, kind, size, sha256).
A ``Manifest`` aggregates components + an optional whole-archive sha256; it is
written BOTH embedded inside the archive and as a sidecar ``<id>.manifest.json``
so remote listing/verify needs no unpack. ``extra="allow"`` on the manifest and
the ``metadata`` dict on components are the plugin *contribution hooks* — a
source may add app-specific fields (bases/records counts, …) without engine
changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Component(BaseModel):
    name: str
    kind: str
    size: int
    sha256: str
    error: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    snapshot_id: str
    instance_name: str
    created_at: str
    total_bytes: int = 0
    archive_sha256: Optional[str] = None
    components: list[Component] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        snapshot_id: str,
        instance_name: str,
        components: list[Component],
        created_at: str,
        archive_sha256: Optional[str] = None,
        **extra: object,
    ) -> "Manifest":
        return cls(
            snapshot_id=snapshot_id,
            instance_name=instance_name,
            created_at=created_at,
            total_bytes=sum(c.size for c in components),
            archive_sha256=archive_sha256,
            components=list(components),
            **extra,
        )


def sidecar_path(directory: Path, snapshot_id: str) -> Path:
    return Path(directory) / f"{snapshot_id}.manifest.json"


def write_manifest(manifest: Manifest, path: Path) -> None:
    Path(path).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def read_manifest(path: Path) -> Manifest:
    return Manifest.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
