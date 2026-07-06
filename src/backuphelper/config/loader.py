"""Layered config loader.

Precedence (highest wins):
  1. discrete env overrides  (BACKUP_<PATH...> with ``__`` separators)
  2. inline JSON             (BACKUP_CONFIG_JSON / BACKUP_CONFIG_JSON_BASE64)
  3. mounted file            (BACKUP_CONFIG_FILE, .json or .yaml)
  4. built-in defaults       (RootConfig field defaults)

``${VAR}`` placeholders in the assembled base are resolved against ``env`` so
secrets stay out of the JSON literal. This mirrors the fleet's init.json
containers (e.g. MinIO minio-init) while adding an inline (no-host-file) path.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml
from pydantic import ValidationError

from .interpolation import MissingEnvVar, interpolate
from .models import RootConfig

# Control vars that select the base config — never treated as path overrides.
_CONTROL_VARS = {"BACKUP_CONFIG_JSON", "BACKUP_CONFIG_JSON_BASE64", "BACKUP_CONFIG_FILE"}
_OVERRIDE_PREFIX = "BACKUP_"
_PATH_SEP = "__"


class ConfigError(ValueError):
    """Raised for malformed or invalid configuration (fail-fast, exit code 2)."""


def load_config(env: Optional[Mapping[str, str]] = None) -> RootConfig:
    env = dict(os.environ if env is None else env)

    base = _load_base(env)
    try:
        base = interpolate(base, env)
    except MissingEnvVar as exc:
        raise ConfigError(f"config references undefined env var: {exc}") from exc

    _apply_overrides(base, env)

    try:
        return RootConfig.model_validate(base)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{exc}") from exc


def _load_base(env: Mapping[str, str]) -> dict[str, Any]:
    """Resolve the base config dict from file first, then inline JSON on top."""
    base: dict[str, Any] = {}

    file_path = env.get("BACKUP_CONFIG_FILE")
    if file_path:
        base = _deep_merge(base, _read_config_file(Path(file_path)))

    inline = _read_inline_json(env)
    if inline is not None:
        base = _deep_merge(base, inline)

    return base


def _read_inline_json(env: Mapping[str, str]) -> Optional[dict[str, Any]]:
    raw = env.get("BACKUP_CONFIG_JSON")
    if raw is None and env.get("BACKUP_CONFIG_JSON_BASE64"):
        try:
            raw = base64.b64decode(env["BACKUP_CONFIG_JSON_BASE64"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(f"BACKUP_CONFIG_JSON_BASE64 is not valid base64: {exc}") from exc
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"BACKUP_CONFIG_JSON is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("BACKUP_CONFIG_JSON must be a JSON object")
    return parsed


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"BACKUP_CONFIG_FILE not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        # yaml.safe_load parses JSON too, so it covers both .json and .yaml.
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"BACKUP_CONFIG_FILE is not valid JSON/YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"BACKUP_CONFIG_FILE must contain a mapping: {path}")
    return parsed


def _apply_overrides(base: dict[str, Any], env: Mapping[str, str]) -> None:
    """Apply BACKUP_<A>__<B>__... = value discrete overrides onto the base tree."""
    for key, value in env.items():
        if not key.startswith(_OVERRIDE_PREFIX) or key in _CONTROL_VARS:
            continue
        if _PATH_SEP not in key:
            continue
        path = [seg.lower() for seg in key[len(_OVERRIDE_PREFIX):].split(_PATH_SEP) if seg]
        if path:
            _set_path(base, path, _coerce(value))


def _coerce(value: str) -> Any:
    """Parse an override value as JSON (numbers/bools/objects) or keep as string."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _set_path(tree: Any, path: list[str], value: Any) -> None:
    cur = tree
    for i, seg in enumerate(path):
        last = i == len(path) - 1
        key: Any = int(seg) if seg.isdigit() else seg
        if last:
            _assign(cur, key, value)
        else:
            nxt = _child(cur, key)
            if nxt is None:
                nxt = [] if (i + 1 < len(path) and path[i + 1].isdigit()) else {}
                _assign(cur, key, nxt)
            cur = nxt


def _child(container: Any, key: Any) -> Any:
    if isinstance(container, list) and isinstance(key, int) and key < len(container):
        return container[key]
    if isinstance(container, dict):
        return container.get(key)
    return None


def _assign(container: Any, key: Any, value: Any) -> None:
    if isinstance(container, list) and isinstance(key, int):
        while len(container) <= key:
            container.append({})
        container[key] = value
    elif isinstance(container, dict):
        container[key] = value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
