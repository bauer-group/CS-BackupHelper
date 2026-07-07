"""``${VAR}`` interpolation for config trees.

Secrets are never written literally into inline JSON; they are referenced as
``${ENV_VAR}`` and resolved after parsing against an *injected* env mapping
(so tests pass a dict instead of mutating ``os.environ``).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


class MissingEnvVar(KeyError):
    """Raised when a ``${VAR}`` placeholder has no value in the env mapping."""


def interpolate(value: Any, env: Mapping[str, str]) -> Any:
    """Recursively replace ``${VAR}`` placeholders in strings within ``value``.

    Non-string leaves (int, bool, None, …) pass through unchanged.
    """
    if isinstance(value, str):
        return _interpolate_str(value, env)
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(item, env) for item in value]
    return value


def _interpolate_str(text: str, env: Mapping[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise MissingEnvVar(name)
        return env[name]

    return _PLACEHOLDER.sub(_replace, text)
