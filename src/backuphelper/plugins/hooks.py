"""Lifecycle hooks — opt-in extension points, empty by default.

The zero-coupling online dump stays the default (no hooks). A consuming repo may
register hooks to quiesce an app, run a pre-restore safety check (e.g. an
ENCRYPTION_KEY cross-check that ABORTS by raising), or run post-restore cleanup.
A raising hook propagates, so pre_* gates can stop the operation.
"""

from __future__ import annotations

from typing import Any, Callable

PHASES = ("pre_backup", "post_backup", "pre_dump", "post_dump", "pre_restore", "post_restore")

Hook = Callable[[Any], None]


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
