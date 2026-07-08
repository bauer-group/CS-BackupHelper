"""CLI command-plugin discovery.

A consuming repo can extend the engine's CLI with app-specific operator
subcommands (e.g. NocoDB's ``restore-schema`` / ``restore-records`` /
``restore-attachments``) by registering a ``typer.Typer`` group under the
``backuphelper.commands`` entry-point group in its own package metadata. The
engine mounts each discovered group under the top-level CLI at startup, so
app-aware restore/export logic lives in the repo's plugin — never in a fork of
the engine. Built-in commands are unaffected; a broken plugin is skipped.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, Callable, Iterable

import typer

ENTRY_POINT_GROUP = "backuphelper.commands"
log = logging.getLogger(__name__)


def _default_load() -> Iterable[tuple[str, Any]]:
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            yield ep.name, ep.load()
        except Exception:  # noqa: BLE001 - a broken plugin must never break the CLI
            log.warning("command plugin %r failed to load", ep.name)


def register_command_plugins(
    app: typer.Typer,
    *,
    load: Callable[[], Iterable[tuple[str, Any]]] = _default_load,
) -> list[str]:
    """Mount every discovered plugin ``Typer`` group under ``app``.

    Each entry point may resolve to a ``typer.Typer`` directly, or to a zero-arg
    factory returning one. Returns the names actually mounted.
    """
    mounted: list[str] = []
    for name, obj in load():
        group = obj
        if callable(group) and not isinstance(group, typer.Typer):
            try:
                group = group()
            except Exception:  # noqa: BLE001
                log.warning("command plugin factory %r raised", name)
                continue
        if isinstance(group, typer.Typer):
            app.add_typer(group, name=name)
            mounted.append(name)
        else:
            log.warning("command plugin %r is not a typer.Typer (got %s)", name, type(group).__name__)
    return mounted
