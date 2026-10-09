"""MariaDB / MySQL logical-dump source.

One client (alpine ``mariadb-client``) covers MariaDB and MySQL via
``mariadb-dump`` (with a ``mysqldump`` fallback). The password is passed via the
``MYSQL_PWD`` environment variable, never on the command line.

mariadb-dump takes every server version >= 10.3 for MariaDB and, with
``--routines``, asks it for MariaDB packages (``SHOW PACKAGE STATUS``). MySQL's
calendar versions (26.x) are above that, so on MySQL 26+ the query is a syntax
error and the whole dump fails. For such a server the routines are dumped in a
second pass, where that one error is expected — see :meth:`MariaDBSource.produce`.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from pydantic import Field

from ..config.models import ConfigModel
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

# The second pass on MySQL 26+: only the routines (functions, procedures).
# --force lets mariadb-dump write them although the package query fails.
# --single-transaction, as in the main pass: without it mariadb-dump runs
# LOCK TABLES on every table (even with --no-data), which blocks writers and
# needs the LOCK TABLES privilege.
_ROUTINES_ONLY_FLAGS = (
    "--single-transaction", "--routines", "--skip-triggers", "--no-create-info",
    "--no-data", "--no-create-db", "--no-tablespaces",
    "--default-character-set=utf8mb4", "--force",
)
_PACKAGE_QUERY = re.compile(r"Couldn't execute 'SHOW PACKAGE (BODY )?STATUS")
# What the client prints that is no error: its TLS notice when the password
# comes from MYSQL_PWD, and the old-name notice when it runs as mysqldump.
_CLIENT_NOTICE = re.compile(r"^(\S+: )?(warning|notice)\b|deprecated program name", re.I)
_EX_MYSQLERR = 2  # mariadb-dump's exit code after an SQL error


class MySQLFamilyConfig(ConfigModel):
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


def _connection(cfg: MySQLFamilyConfig) -> list[str]:
    return ["--host", cfg.host, "--port", str(cfg.port), "--user", cfg.user]


def _targets(cfg: MySQLFamilyConfig) -> list[str]:
    if cfg.databases:
        return ["--databases", *cfg.databases]
    return [cfg.database] if cfg.database else []


def build_argv(cfg: MySQLFamilyConfig, binary: str, routines: bool = True) -> list[str]:
    flags = [f if routines or f != "--routines" else "--skip-routines" for f in _DUMP_FLAGS]
    return [binary, *flags, *_connection(cfg), *_targets(cfg)]


def build_routines_argv(cfg: MySQLFamilyConfig, binary: str) -> list[str]:
    return [binary, *_ROUTINES_ONLY_FLAGS, *_connection(cfg), *_targets(cfg)]


def needs_separate_routines(server_version: str) -> bool:
    """True for a MySQL server whose version mariadb-dump mistakes for MariaDB
    >= 10.3 (MySQL 26+); MariaDB reports "...-MariaDB" in its version."""
    match = re.match(r"(\d+)\.", server_version.strip())
    return bool(match) and int(match.group(1)) >= 10 and "mariadb" not in server_version.lower()


def only_package_errors(result: subprocess.CompletedProcess) -> bool:
    """A routines-only pass that failed solely on the MariaDB package query.

    Fails closed: with --force, mariadb-dump reports a problem and goes on, and
    not every report says "error" — a routine whose body the user may not read
    "has insufficient privileges" and is left out. So every stderr line other
    than the package query and the client's notices counts as an error."""
    lines = [line for line in (result.stderr or b"").decode("utf-8", "replace").splitlines()
             if line.strip()]
    package = [line for line in lines if _PACKAGE_QUERY.search(line)]
    other = [line for line in lines
             if not _PACKAGE_QUERY.search(line) and not _CLIENT_NOTICE.search(line)]
    return result.returncode == _EX_MYSQLERR and bool(package) and not other


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

    @property
    def component_name(self) -> str:
        return self.cfg.component_name()

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        out = staging_dir / f"{self.cfg.component_name()}.sql.gz"
        binary = resolve_binary(self.cfg, self._which)
        meta = {"engine": self.type, "binary": Path(binary).name}
        env = self.build_env()
        try:
            separate = needs_separate_routines(self._server_version(env))
            result = self._run(build_argv(self.cfg, binary, routines=not separate),
                               env=env, capture_output=True, timeout=self.cfg.timeout)
            if result.returncode != 0:
                return [self._error(out, result.stderr, meta)]
            dump = result.stdout or b""
            if separate:
                # The tables, triggers and events are complete; the routines
                # follow as a dump of their own (header, USE, footer included).
                routines = self._run(build_routines_argv(self.cfg, binary),
                                     env=env, capture_output=True, timeout=self.cfg.timeout)
                if routines.returncode != 0 and not only_package_errors(routines):
                    return [self._error(out, routines.stderr, meta)]
                dump += routines.stdout or b""
                meta["routines"] = "separate pass (MySQL 26+)"
        except subprocess.TimeoutExpired:
            return [self._error(out, b"dump timed out", meta)]
        with gzip.open(out, "wb", compresslevel=6) as gz:
            gz.write(dump)
        return [StagedComponent(name=self.cfg.component_name(), kind=self.type, path=out, metadata=meta)]

    def _server_version(self, env: dict[str, str]) -> str:
        """``SELECT VERSION()`` of the server, or "" if the query fails (the
        dump then runs as always and reports the real connection error)."""
        argv = [self._restore_binary(), *_connection(self.cfg), "--batch",
                "--skip-column-names", "--execute", "SELECT VERSION()"]
        try:
            result = self._run(argv, env=env, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or b"").decode("utf-8", "replace").strip()

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
        argv = [binary, *_connection(self.cfg)]
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
