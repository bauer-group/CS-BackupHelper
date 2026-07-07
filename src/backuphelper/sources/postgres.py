"""PostgreSQL source — pg_dump (custom / plain) + pg_restore/psql restore.

The password goes into the subprocess environment (PGPASSWORD), never onto the
command line, so it never appears in ``ps`` output.
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


class PostgresConfig(BaseModel):
    host: str = "database-server"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = "postgres"
    user: str = "postgres"
    password: str = ""
    ssl_mode: str = "disable"
    dump_format: str = "custom"  # custom | plain
    timeout: int = Field(default=1800, ge=1, le=14400)
    name: Optional[str] = None  # component name; defaults to the database name

    def component_name(self) -> str:
        return self.name or self.database or "database"


def build_env(cfg: PostgresConfig) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        PGHOST=cfg.host,
        PGPORT=str(cfg.port),
        PGDATABASE=cfg.database,
        PGUSER=cfg.user,
        PGPASSWORD=cfg.password,
        PGSSLMODE=cfg.ssl_mode,
    )
    return env


def build_dump_argv(cfg: PostgresConfig, out_path: Path) -> list[str]:
    if cfg.dump_format == "custom":
        return [
            "pg_dump", "--format=custom", "--compress=6",
            "--no-owner", "--no-acl", "--file", str(out_path),
        ]
    return ["pg_dump", "--format=plain", "--no-owner", "--no-acl"]


class PostgresSource(Source):
    type = "postgres"

    def __init__(self, spec: Mapping[str, Any], run: RunFn = subprocess.run):
        super().__init__(spec)
        self.cfg = PostgresConfig.model_validate(_normalize(spec))
        self._run = run

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".dump" if self.cfg.dump_format == "custom" else ".sql.gz"
        out = staging_dir / f"{self.cfg.component_name()}{suffix}"
        env = build_env(self.cfg)
        argv = build_dump_argv(self.cfg, out)
        meta = {"format": self.cfg.dump_format, "database": self.cfg.database}
        try:
            if self.cfg.dump_format == "custom":
                result = self._run(argv, env=env, capture_output=True, timeout=self.cfg.timeout)
                if result.returncode != 0:
                    return [self._error(out, result.stderr, meta)]
            else:
                result = self._run(argv, env=env, capture_output=True, timeout=self.cfg.timeout)
                if result.returncode != 0:
                    return [self._error(out, result.stderr, meta)]
                with gzip.open(out, "wb", compresslevel=6) as gz:
                    gz.write(result.stdout or b"")
        except subprocess.TimeoutExpired:
            return [self._error(out, b"pg_dump timed out", meta)]
        return [StagedComponent(name=self.cfg.component_name(), kind=self.type, path=out, metadata=meta)]

    def _error(self, out: Path, stderr: bytes, meta: dict) -> StagedComponent:
        out.unlink(missing_ok=True)
        msg = (stderr or b"").decode("utf-8", "replace").strip()[:500] or "pg_dump failed"
        return StagedComponent(name=self.cfg.component_name(), kind=self.type, path=None,
                               metadata=meta, error=f"pg_dump failed: {msg}")

    def restore(self, staged_dir: Path) -> None:
        dumps = sorted(Path(staged_dir).glob(f"{self.cfg.component_name()}.*"))
        if not dumps:
            raise SourceError(f"no {self.cfg.component_name()}.* dump found in {staged_dir}")
        _pg_restore(self.cfg, dumps[0], self._run)


def build_restore_argv(cfg: PostgresConfig, dump: Path) -> list[str]:
    suffix = "".join(dump.suffixes)
    if suffix.endswith(".dump"):
        return ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
                "--single-transaction", "--dbname", cfg.database, str(dump)]
    if suffix.endswith(".sql.gz"):
        return ["psql", "--quiet"]  # dump is streamed to stdin (gunzipped)
    return ["psql", "--quiet", "--file", str(dump)]


def _pg_restore(cfg: PostgresConfig, dump: Path, run: RunFn) -> None:
    env = build_env(cfg)
    argv = build_restore_argv(cfg, dump)
    if "".join(dump.suffixes).endswith(".sql.gz"):
        # gunzip to a real temp file: a subprocess reads the child's stdin fd
        # directly, so a gzip file object would feed it the *compressed* bytes.
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
            tmp_name = tmp.name
            with gzip.open(dump, "rb") as gz:
                shutil.copyfileobj(gz, tmp)
        try:
            with open(tmp_name, "rb") as fh:
                result = run(argv, env=env, stdin=fh, capture_output=True, timeout=14400)
        finally:
            os.unlink(tmp_name)
    else:
        result = run(argv, env=env, capture_output=True, timeout=14400)
    if result.returncode != 0:
        msg = (result.stderr or b"").decode("utf-8", "replace").strip()[:500]
        raise SourceError(f"postgres restore failed: {msg}")


def _normalize(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Accept ``db`` as an alias for ``database`` and drop null keys."""
    out = {k: v for k, v in spec.items() if k != "type" and v is not None}
    if "database" not in out and "db" in out:
        out["database"] = out.pop("db")
    out.pop("db", None)
    return out
