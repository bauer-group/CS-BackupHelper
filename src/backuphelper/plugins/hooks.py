"""Lifecycle hooks — opt-in extension points, empty by default.

The zero-coupling online dump stays the default (no hooks). A consuming repo may
register hooks to quiesce an app, run a pre-restore safety check (e.g. an
ENCRYPTION_KEY cross-check that ABORTS by raising), or run post-restore cleanup.
A raising hook propagates, so pre_* gates can stop the operation.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, Callable, Iterable

PHASES = ("pre_backup", "post_backup", "pre_dump", "post_dump", "pre_restore", "post_restore")

Hook = Callable[[Any], None]

ENTRY_POINT_GROUP = "backuphelper.hooks"
log = logging.getLogger(__name__)


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = {phase: [] for phase in PHASES}

    def register(self, phase: str, hook: Hook) -> None:
        if phase not in self._hooks:
            raise ValueError(f"unknown hook phase: {phase!r}; valid: {PHASES}")
        self._hooks[phase].append(hook)

    def run(self, phase: str, context: Any = None) -> None:
        for hook in self._hooks.get(phase, ()):
            hook(context)


def _default_load() -> Iterable[tuple[str, Callable[["HookRegistry"], None]]]:
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            yield ep.name, ep.load()
        except Exception:  # noqa: BLE001 - a broken hook plugin must not break discovery
            log.warning("hook plugin %r failed to load", ep.name)


def discover_hooks(*, load: Callable[[], Iterable[tuple[str, Any]]] = _default_load) -> HookRegistry:
    """Build a HookRegistry from the ``backuphelper.hooks`` entry-point group.

    Each entry point resolves to a ``register(registry)`` callable that the plugin
    uses to register its own phase hooks (e.g. a ``pre_restore`` ENCRYPTION_KEY
    gate or a ``pre_backup`` app-quiesce). With no hook plugins installed the
    registry is empty, so the zero-coupling default is unchanged. A plugin whose
    ``register`` raises is skipped, not fatal."""
    registry = HookRegistry()
    for name, register_fn in load():
        try:
            register_fn(registry)
        except Exception:  # noqa: BLE001
            log.warning("hook plugin %r failed to register", name)
    return registry
