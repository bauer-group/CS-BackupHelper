"""Layered config loader.

Precedence (highest wins):
  1. discrete env overrides  (BACKUP_<PATH...> with ``__`` separators)
  2. inline JSON             (BACKUP_CONFIG_JSON / BACKUP_CONFIG_JSON_BASE64)
  3. mounted file            (BACKUP_CONFIG_FILE, .json or .yaml)
  4. built-in defaults       (RootConfig field defaults)

``${VAR}`` placeholders in the assembled base are resolved against ``env`` so
secrets stay out of the JSON literal. This mirrors the fleet's init.json
containers (e.g. MinIO minio-init) while adding an inline (no-host-file) path.

A discrete override is typed by the field it targets (:func:`_override_kind`):
a text field takes the value verbatim, so a numeric-looking secret stays text.
"""

from __future__ import annotations

import base64
import json
import os
import types
import typing
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Union

import yaml
from pydantic import BaseModel, ValidationError

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
            _set_path(base, path, _coerce(value, _override_kind(path)))


# How an override value is read, by the type of the field it targets:
_TEXT = "text"  # a str field: verbatim, so "20261006", "1e5" and "null" stay text
_JSON = "json"  # a number, bool, list or object field: parsed as JSON when it is JSON
_OPEN = "open"  # a key the engine does not type (a source's or destination's own)


def _coerce(value: str, kind: str = _JSON) -> Any:
    """The override value for a field of the given kind (see above).

    For an open key only unambiguous JSON is parsed - true / false / null, an
    array or an object; anything else, numbers included, stays text, because
    the receiving model may declare text (a password, a user or bucket name)
    and pydantic turns "5432" into 5432 where a number is declared, but never
    20261006 back into "20261006". Every built-in source, the S3 destination
    and the runner's "enabled" toggle read numbers given as text."""
    if kind == _TEXT:
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if kind == _JSON or parsed is None or isinstance(parsed, (bool, list, dict)):
        return parsed
    return value


def _override_kind(path: list[str]) -> str:
    """Follow ``path`` through the config models to the field it sets.

    ``jobs`` → a list item (numeric segment) → ``Job`` → ``retention`` →
    ``count``: an int field, so the value is parsed. A segment below a model
    field the engine does not declare (``jobs__0__sources__0__password`` - the
    source spec keeps its own keys open for plugins) makes the key open."""
    ann: Any = RootConfig
    model: Any = RootConfig
    for seg in path:
        if model is None:
            return _OPEN
        if isinstance(model, type) and issubclass(model, BaseModel):
            field = model.model_fields.get(seg)
            if field is None:
                return _OPEN
            ann = field.annotation
        elif seg.isdigit() and typing.get_origin(model) in (list, tuple):
            ann = (typing.get_args(model) or (None,))[0]
        else:
            return _OPEN
        model = _container(ann)
    return _TEXT if _is_text(ann) else _JSON


def _without_none(ann: Any) -> Any:
    if typing.get_origin(ann) in (Union, types.UnionType):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return args[0] if len(args) == 1 else ann
    return ann


def _container(ann: Any) -> Any:
    """The annotation a further path segment indexes into: a model or a list,
    else None (the path would go below a scalar)."""
    ann = _without_none(ann)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    if typing.get_origin(ann) in (list, tuple):
        return ann
    return None


def _is_text(ann: Any) -> bool:
    ann = _without_none(ann)
    if ann is str:
        return True
    if typing.get_origin(ann) is Literal:
        return all(isinstance(arg, str) for arg in typing.get_args(ann))
    return False


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
