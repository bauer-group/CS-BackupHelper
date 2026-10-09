"""PostgreSQL source — pg_dump (custom / plain) + pg_restore/psql restore.

The password goes into the subprocess environment (PGPASSWORD), never onto the
command line, so it never appears in ``ps`` output.

A custom-format restore runs ``pg_restore --clean --if-exists`` in one
transaction. That clean phase cannot handle partitioned tables that exist in
the target database: it drops each partition's primary key on its own, which
PostgreSQL refuses (``cannot drop inherited constraint``), so the whole restore
rolls back. When the target holds partitioned tables the dump recreates, the
restore therefore drops those tables first, in the same transaction as the
dump's own clean-and-create script — see :func:`_restore_replacing`.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from pydantic import Field, field_validator

from ..config.models import ConfigModel
from .base import Source, SourceError, StagedComponent

RunFn = Callable[..., subprocess.CompletedProcess]

log = logging.getLogger(__name__)

RESTORE_TIMEOUT = 14400  # seconds
CATALOG_TIMEOUT = 300  # seconds, for the catalog query and the dump's TOC

# Partitioned tables that are not themselves a partition (dropping one takes its
# partitions with it), with PostgreSQL's own identifier quoting for the DROP.
LIVE_PARTITIONED_SQL = (
    "SELECT n.nspname, c.relname, format('%I.%I', n.nspname, c.relname) "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'p' AND NOT c.relispartition "
    "ORDER BY 1, 2"
)

# A table in `pg_restore --list`: "<id>; 1259 <oid> TABLE <schema> <name> <owner>".
# 1259 is pg_class: "TABLE DATA" / "TABLE ATTACH" entries carry 0 instead. Names
# are printed unquoted, so they are matched by prefix rather than split on spaces.
_TOC_TABLE = re.compile(r"^\d+;\s+1259\s+\d+\s+TABLE\s(.*)$")


class PostgresConfig(ConfigModel):
    host: str = "database-server"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = "postgres"
    user: str = "postgres"
    password: str = ""
    ssl_mode: str = "disable"
    dump_format: str = "custom"  # custom | plain
    timeout: int = Field(default=1800, ge=1, le=14400)
    name: Optional[str] = None  # component name; defaults to the database name
    # Tables whose ROW DATA is dropped from the dump while the STRUCTURE is kept
    # (pg_dump --exclude-table-data) — e.g. n8n execution history: restore the
    # empty tables, not the bulky rows. Accepts a CSV string or a list.
    exclude_table_data: list[str] = Field(default_factory=list)

    @field_validator("exclude_table_data", mode="before")
    @classmethod
    def _csv_or_list(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

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
    # Keep each excluded table's schema but drop its data (structure-only).
    exclude = [f"--exclude-table-data={t}" for t in cfg.exclude_table_data]
    if cfg.dump_format == "custom":
        return [
            "pg_dump", "--format=custom", "--compress=6",
            "--no-owner", "--no-acl", *exclude, "--file", str(out_path),
        ]
    return ["pg_dump", "--format=plain", "--no-owner", "--no-acl", *exclude]


class PostgresSource(Source):
    type = "postgres"

    def __init__(self, spec: Mapping[str, Any], run: RunFn = subprocess.run):
        super().__init__(spec)
        self.cfg = PostgresConfig.model_validate(_normalize(spec))
        self._run = run

    @property
    def component_name(self) -> str:
        return self.cfg.component_name()

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


def _clean_restore_flags(cfg: PostgresConfig) -> list[str]:
    return ["--clean", "--if-exists", "--no-owner", "--no-acl"]


def build_restore_argv(cfg: PostgresConfig, dump: Path) -> list[str]:
    suffix = "".join(dump.suffixes)
    if suffix.endswith(".dump"):
        return ["pg_restore", *_clean_restore_flags(cfg),
                "--single-transaction", "--dbname", cfg.database, str(dump)]
    # ON_ERROR_STOP=1 makes psql exit non-zero on the first failed statement
    # instead of swallowing errors and exiting 0 — so a plain-SQL restore onto a
    # non-empty DB fails loudly instead of reporting a false success.
    if suffix.endswith(".sql.gz"):
        return ["psql", "--quiet", "--set", "ON_ERROR_STOP=1"]  # dump streamed to stdin (gunzipped)
    return ["psql", "--quiet", "--set", "ON_ERROR_STOP=1", "--file", str(dump)]


def _pg_restore(cfg: PostgresConfig, dump: Path, run: RunFn) -> None:
    env = build_env(cfg)
    if "".join(dump.suffixes).endswith(".dump"):
        tables = partitioned_tables_to_replace(dump, env, run)
        if tables:
            _restore_replacing(cfg, dump, tables, env, run)
            return
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
                result = run(argv, env=env, stdin=fh, capture_output=True, timeout=RESTORE_TIMEOUT)
        finally:
            os.unlink(tmp_name)
    else:
        result = run(argv, env=env, capture_output=True, timeout=RESTORE_TIMEOUT)
    if result.returncode != 0:
        raise SourceError(f"postgres restore failed: {_text(result.stderr)}")


def partitioned_tables_to_replace(dump: Path, env: dict[str, str], run: RunFn) -> list[str]:
    """Quoted names of the target's partitioned tables that the dump recreates.

    Empty (the plain ``pg_restore`` path) when the target has no partitioned
    tables — a fresh database, or an application without partitioning.
    """
    live = run(
        ["psql", "--no-psqlrc", "--quiet", "--tuples-only", "--no-align",
         "--field-separator-zero", "--record-separator-zero",
         "--set", "ON_ERROR_STOP=1", "--command", LIVE_PARTITIONED_SQL],
        env=env, capture_output=True, timeout=CATALOG_TIMEOUT,
    )
    if live.returncode != 0:
        raise SourceError("postgres restore failed: could not list the partitioned tables "
                          f"of the target database: {_text(live.stderr)}")
    candidates = _nul_rows(live.stdout, columns=3)
    if not candidates:
        return []
    listing = run(["pg_restore", "--list", str(dump)],
                  env=env, capture_output=True, timeout=CATALOG_TIMEOUT)
    if listing.returncode != 0:
        raise SourceError("postgres restore failed: could not read the table of contents "
                          f"of {dump.name}: {_text(listing.stderr)}")
    in_dump = dump_table_entries(_text(listing.stdout, limit=None))
    return [quoted for schema, name, quoted in candidates
            if any(entry.startswith(f"{schema} {name} ") for entry in in_dump)]


def dump_table_entries(listing: str) -> list[str]:
    """``"<schema> <name> <owner> "`` of every table in a ``pg_restore --list``."""
    entries = []
    for line in listing.splitlines():
        match = _TOC_TABLE.match(line)
        if match:
            entries.append(match.group(1) + " ")
    return entries


def _restore_replacing(cfg: PostgresConfig, dump: Path, tables: list[str],
                       env: dict[str, str], run: RunFn) -> None:
    """Drop ``tables`` and run the dump's clean-and-create script, in ONE
    transaction: any error rolls everything back, the drops included."""
    # A file, not a pipe: if pg_restore stopped half-way, psql would commit a
    # truncated script. The script is complete before anything is executed.
    with tempfile.TemporaryDirectory(dir=dump.parent) as tmp:
        prelude = Path(tmp) / "drop-partitioned.sql"
        # CASCADE also drops what depends on the table (foreign keys, views): the
        # dump recreates everything it contains, and psql's notices (logged
        # below) name each object it dropped.
        prelude.write_text("".join(f"DROP TABLE IF EXISTS {t} CASCADE;\n" for t in tables),
                           encoding="utf-8")
        script = Path(tmp) / "restore.sql"
        generate = run(["pg_restore", *_clean_restore_flags(cfg), "--file", str(script), str(dump)],
                       env=env, capture_output=True, timeout=RESTORE_TIMEOUT)
        if generate.returncode != 0:
            raise SourceError(f"postgres restore failed: {_text(generate.stderr)}")
        log.info("restoring %s over %d partitioned table(s) of the target database: %s",
                 dump.name, len(tables), ", ".join(tables))
        apply = run(["psql", "--no-psqlrc", "--quiet", "--single-transaction",
                     "--set", "ON_ERROR_STOP=1", "--file", str(prelude), "--file", str(script)],
                    env=env, capture_output=True, timeout=RESTORE_TIMEOUT)
        if apply.returncode != 0:
            raise SourceError(f"postgres restore failed: {_psql_error(apply.stderr)}")
        for line in _text(apply.stderr, limit=None).splitlines():
            if line.strip():
                log.info("postgres restore: %s", line.strip())


def _nul_rows(data: bytes | str | None, columns: int) -> list[tuple[str, ...]]:
    """Rows of psql's unaligned output with NUL field and record separators:
    a name can hold any character but NUL, which no identifier contains."""
    raw = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    fields = raw.split("\0")
    if fields and not fields[-1].strip():
        fields.pop()  # the separator after the last record
    return [tuple(fields[i:i + columns]) for i in range(0, len(fields) - columns + 1, columns)]


def _psql_error(stderr: bytes | str | None) -> str:
    """psql's ERROR/FATAL lines — the prelude's notices come first in stderr
    and would otherwise crowd the actual error out of the message."""
    lines = _text(stderr, limit=None).splitlines()
    errors = [line.strip() for line in lines if "ERROR:" in line or "FATAL:" in line]
    return "\n".join(errors or lines)[:500]


def _text(data: bytes | str | None, limit: int | None = 500) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    text = text.strip()
    return text if limit is None else text[:limit]


def _normalize(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Accept ``db`` as an alias for ``database`` and drop null keys."""
    out = {k: v for k, v in spec.items() if k != "type" and v is not None}
    if "database" not in out and "db" in out:
        out["database"] = out.pop("db")
    out.pop("db", None)
    return out
