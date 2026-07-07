"""Source plugin registry.

Built-in sources are always available. Consuming repos register additional
sources via the ``backuphelper.sources`` entry-point group (declared in their
own package metadata) — the engine discovers them here without hardcoding any
app taxonomy. Built-ins win over plugins of the same name.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Mapping

from ..sources.base import Source
from ..sources.env_snapshot import EnvSnapshotSource
from ..sources.filesystem import FilesystemSource
from ..sources.mariadb import MariaDBSource
from ..sources.mysql import MySQLSource
from ..sources.postgres import PostgresSource
from ..sources.s3_bucket import S3BucketSource

ENTRY_POINT_GROUP = "backuphelper.sources"

BUILTIN_SOURCES: dict[str, type[Source]] = {
    PostgresSource.type: PostgresSource,
    MariaDBSource.type: MariaDBSource,
    MySQLSource.type: MySQLSource,
    S3BucketSource.type: S3BucketSource,
    FilesystemSource.type: FilesystemSource,
    EnvSnapshotSource.type: EnvSnapshotSource,
}


class SourceNotFound(KeyError):
    """No built-in or plugin source matches the requested type."""


def _load_plugin_sources() -> dict[str, type[Source]]:
    found: dict[str, type[Source]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[ep.name] = ep.load()
        except Exception:  # noqa: BLE001 - a broken plugin must not break discovery
            continue
    return found


def get_source_class(
    type_name: str,
    *,
    builtins: Mapping[str, type[Source]] = BUILTIN_SOURCES,
    load_plugins: Callable[[], Mapping[str, type[Source]]] = _load_plugin_sources,
) -> type[Source]:
    if type_name in builtins:
        return builtins[type_name]
    plugins = load_plugins()
    if type_name in plugins:
        return plugins[type_name]
    raise SourceNotFound(type_name)


def build_source(spec: Mapping[str, Any], **kwargs: Any) -> Source:
    cls = get_source_class(spec["type"], **kwargs)
    return cls(spec)
