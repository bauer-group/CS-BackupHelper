"""MariaDB / MySQL logical-dump source.

One client (alpine ``mariadb-client``) covers MariaDB 11/12 and MySQL 8/9 via
``mariadb-dump`` (with a ``mysqldump`` fallback). The password is passed via the
``MYSQL_PWD`` environment variable, never on the command line.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from pydantic import BaseModel, Field

from .base import Source, SourceError, StagedComponent

RunFn = Callable[..., subprocess.CompletedProcess]
WhichFn = Callable[[str], Optional[str]]

# Binary preference per source type; first found wins.
_BINARY_PREFERENCE = {
    "mariadb": ("mariadb-dump", "mysqldump"),
    "mysql": ("mysqldump", "mariadb-dump"),
}
# Restore uses the interactive client (not the -dump tool).
_RESTORE_BINARY_PREFERENCE = {
    "mariadb": ("mariadb", "mysql"),
    "mysql": ("mysql", "mariadb"),
}

_DUMP_FLAGS = (
    "--single-transaction", "--quick", "--routines", "--triggers",
    "--events", "--no-tablespaces", "--default-character-set=utf8mb4",
)


class MySQLFamilyConfig(BaseModel):
    kind: str = "mariadb"
    host: str = "database"
    port: int = Field(default=3306, ge=1, le=65535)
    database: Optional[str] = None
    databases: list[str] = Field(default_factory=list)
    user: str = "root"
    password: str = ""
    binary: Optional[str] = None  # explicit override
    name: Optional[str] = None  # component name; defaults to the database name
    timeout: int = Field(default=2700, ge=1, le=14400)

    def component_name(self) -> str:
        return self.name or self.database or "database"


def resolve_binary(cfg: MySQLFamilyConfig, which: WhichFn = shutil.which) -> str:
    if cfg.binary:
        return cfg.binary
    for candidate in _BINARY_PREFERENCE.get(cfg.kind, ("mariadb-dump",)):
        found = which(candidate)
        if found:
            return found
    return _BINARY_PREFERENCE.get(cfg.kind, ("mariadb-dump",))[0]


def build_argv(cfg: MySQLFamilyConfig, binary: str) -> list[str]:
    argv = [binary, *_DUMP_FLAGS, "--host", cfg.host, "--port", str(cfg.port), "--user", cfg.user]
    if cfg.databases:
        argv += ["--databases", *cfg.databases]
    elif cfg.database:
        argv.append(cfg.database)
    return argv


class MariaDBSource(Source):
    type = "mariadb"

    def __init__(self, spec: Mapping[str, Any], run: RunFn = subprocess.run,
                 which: WhichFn = shutil.which):
        super().__init__(spec)
        data = {k: v for k, v in spec.items() if k not in ("type",) and v is not None}
        data.setdefault("kind", self.type)
        self.cfg = MySQLFamilyConfig.model_validate(data)
        self._run = run
        self._which = which

    def build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MYSQL_PWD"] = self.cfg.password
        return env

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        out = staging_dir / f"{self.cfg.component_name()}.sql.gz"
        binary = resolve_binary(self.cfg, self._which)
        argv = build_argv(self.cfg, binary)
        meta = {"engine": self.type, "binary": Path(binary).name}
        try:
            result = self._run(argv, env=self.build_env(), capture_output=True, timeout=self.cfg.timeout)
        except subprocess.TimeoutExpired:
            return [self._error(out, b"dump timed out", meta)]
        if result.returncode != 0:
            return [self._error(out, result.stderr, meta)]
        with gzip.open(out, "wb", compresslevel=6) as gz:
            gz.write(result.stdout or b"")
        return [StagedComponent(name=self.cfg.component_name(), kind=self.type, path=out, metadata=meta)]

    def _error(self, out: Path, stderr: bytes, meta: dict) -> StagedComponent:
        out.unlink(missing_ok=True)
        msg = (stderr or b"").decode("utf-8", "replace").strip()[:500] or "dump failed"
        return StagedComponent(name=self.cfg.component_name(), kind=self.type, path=None,
                               metadata=meta, error=f"{self.type}-dump failed: {msg}")

    def _restore_binary(self) -> str:
        for candidate in _RESTORE_BINARY_PREFERENCE.get(self.type, ("mariadb",)):
            found = self._which(candidate)
            if found:
                return found
        return _RESTORE_BINARY_PREFERENCE.get(self.type, ("mariadb",))[0]

    def restore(self, staged_dir: Path) -> None:
        dumps = sorted(Path(staged_dir).glob(f"{self.cfg.component_name()}.sql.gz"))
        if not dumps:
            raise SourceError(f"no {self.cfg.component_name()}.sql.gz found in {staged_dir}")
        binary = self._restore_binary()
        argv = [binary, "--host", self.cfg.host, "--port", str(self.cfg.port),
                "--user", self.cfg.user]
        if self.cfg.database:
            argv.append(self.cfg.database)
        # gunzip to a real temp file — a subprocess reads the child's stdin fd
        # directly, so a gzip file object would feed it the *compressed* bytes.
        result = _run_with_gunzipped_stdin(argv, self.build_env(), dumps[0], self._run)
        if result.returncode != 0:
            msg = (result.stderr or b"").decode("utf-8", "replace").strip()[:500]
            raise SourceError(f"{self.type} restore failed: {msg}")


def _run_with_gunzipped_stdin(argv: list[str], env: dict[str, str], gz_path: Path,
                              run: RunFn) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
        tmp_name = tmp.name
        with gzip.open(gz_path, "rb") as gz:
            shutil.copyfileobj(gz, tmp)
    try:
        with open(tmp_name, "rb") as fh:
            return run(argv, env=env, stdin=fh, capture_output=True, timeout=14400)
    finally:
        os.unlink(tmp_name)
