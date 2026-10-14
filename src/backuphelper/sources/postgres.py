"""PostgreSQL source — pg_dump (custom / plain) + pg_restore/psql restore.

The password goes into the subprocess environment (PGPASSWORD), never onto the
command line, so it never appears in ``ps`` output.

A custom-format restore runs ``pg_restore --clean --if-exists`` in one
transaction. That clean phase drops only the dump's own objects, which fails on
two kinds of target, and the whole restore rolls back:

* partitioned tables the dump recreates: it drops each partition's primary key
  on its own, which PostgreSQL refuses (``cannot drop inherited constraint``);
* a schema that moved on after the snapshot — a newer application version's
  migrations added tables, views, foreign keys or types. The dump does not know
  them, so one that depends on the dump's objects blocks their drop (``cannot
  drop index … because other objects depend on it``).

For such a target the restore replaces the snapshot's schemas instead: it drops
their relations, the types they hold that the dump does not, and the schemas
the dump creates first, in the same transaction as the dump's own
clean-and-create script — see :func:`_restore_replacing`. What the upgrade added
there is gone afterwards — functions and the like it added to ``public`` aside —
so the application finds the snapshot's schema and migrates it again.
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
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional

from pydantic import Field, field_validator

from ..config.models import ConfigModel
from .base import Source, SourceError, StagedComponent

RunFn = Callable[..., subprocess.CompletedProcess]

log = logging.getLogger(__name__)

RESTORE_TIMEOUT = 14400  # seconds
CATALOG_TIMEOUT = 300  # seconds, for the catalog query and the dump's TOC

# The target's objects a restore compares with the dump, outside the system
# schemas, as "kind, schema, name, constraint, quoted name, referenced schema":
# * relations (kind: pg_class.relkind) the restoring role may drop - not
#   partitions (dropping the parent takes them along), not an extension's
#   members (CREATE EXTENSION brings them back), not a sequence that belongs to
#   a column (it goes with its table);
# * foreign keys ("F", schema and name of the key's table, with the referenced
#   table's schema) - not a partition's copy of its parent's, not one between an
#   extension's member tables (pg_dump never writes those);
# * enum, range and composite types ("T") and domains ("D") the restoring role
#   may drop, not an extension's;
# * the cluster's roles ("R", the role as name): the owners `pg_restore --list`
#   prints after each entry, whose names may hold spaces.
# Quoting is PostgreSQL's own, for the DROP.
LIVE_OBJECTS_SQL = (
    "SELECT c.relkind::text, n.nspname, c.relname, '', format('%I.%I', n.nspname, c.relname), '' "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND NOT c.relispartition "
    "AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
    "AND pg_catalog.pg_has_role(c.relowner, 'USAGE') "
    "AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend d "
    "WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid "
    "AND (d.deptype = 'e' OR (c.relkind = 'S' AND d.deptype IN ('a', 'i') "
    "AND d.refclassid = 'pg_catalog.pg_class'::regclass))) "
    "UNION ALL "
    "SELECT 'F', n.nspname, c.relname, con.conname, format('%I.%I', n.nspname, c.relname), "
    "rn.nspname "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_class r ON r.oid = con.confrelid "
    "JOIN pg_catalog.pg_namespace rn ON rn.oid = r.relnamespace "
    "WHERE con.contype = 'f' AND con.conparentid = 0 "
    "AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
    "AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend d "
    "WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e') "
    "UNION ALL "
    "SELECT CASE t.typtype WHEN 'd' THEN 'D' ELSE 'T' END, n.nspname, t.typname, '', "
    "format('%I.%I', n.nspname, t.typname), '' "
    "FROM pg_catalog.pg_type t "
    "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
    "LEFT JOIN pg_catalog.pg_class c ON c.oid = t.typrelid "
    "WHERE (t.typtype IN ('e', 'r', 'd') OR (t.typtype = 'c' AND c.relkind = 'c')) "
    "AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
    "AND pg_catalog.pg_has_role(t.typowner, 'USAGE') "
    "AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend d "
    "WHERE d.classid = 'pg_catalog.pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e') "
    "UNION ALL "
    "SELECT 'R', '', rolname, '', '', '' FROM pg_catalog.pg_roles "
    "ORDER BY 1, 2, 3, 4"
)

# Object kind -> its entry type in `pg_restore --list`, which is also the object
# type of its DROP statement.
_OBJECT_TYPES = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW",
                 "f": "FOREIGN TABLE", "S": "SEQUENCE", "T": "TYPE", "D": "DOMAIN",
                 "F": "FK CONSTRAINT"}
_RELATION_TYPES = ("TABLE", "VIEW", "MATERIALIZED VIEW", "FOREIGN TABLE", "SEQUENCE")
# The prelude drops each type in one statement, in this order - what depends on
# something mostly comes first. PostgreSQL then reports ("drop cascades to …")
# the objects outside the dropped ones, and few of those.
_DROP_ORDER = ("MATERIALIZED VIEW", "VIEW", "FOREIGN TABLE", "TABLE", "SEQUENCE",
               "DOMAIN", "TYPE")

# An object in `pg_restore --list`: "<id>; <catalog> <oid> <type> <schema> <name>
# <owner>" - catalog 1247 is pg_type, 1259 pg_class, 2606 pg_constraint (whose
# <name> is "<table> <constraint>"), 2615 pg_namespace (schema "-", the schema's
# name as <name>). "TABLE DATA" / "TABLE ATTACH" entries carry 0 instead. Names
# are printed unquoted; the owner is the last field, possibly with spaces.
_TOC_ENTRY = re.compile(r"^\d+;\s+(?:1247|1259|2606|2615)\s+\d+\s+(.*)$")


class LiveObject(NamedTuple):
    kind: str  # pg_class.relkind, "F" foreign key, "T" type, "D" domain, "R" role
    schema: str
    name: str  # the object, or the foreign key's table
    constraint: str  # the foreign key's name, empty otherwise
    quoted: str  # schema.name as PostgreSQL quotes it
    ref_schema: str  # the schema of the table a foreign key references

    def key(self) -> str:
        """The object as ``dump_keys`` lists it."""
        key = f"{_OBJECT_TYPES[self.kind]} {self.schema} {self.name}"
        return f"{key} {self.constraint}" if self.kind == "F" else key

    def label(self) -> str:
        if self.kind == "F":
            return f"foreign key {self.constraint} on {self.quoted}"
        return {"T": "type ", "D": "domain "}.get(self.kind, "") + self.quoted


class Replacement(NamedTuple):
    """What a restore drops before the dump's own script, and why."""
    relations: list[LiveObject]  # every relation of the snapshot's schemas
    types: list[LiveObject]  # types and domains of those schemas the dump lacks
    schemas: list[str]  # the snapshot's schemas
    recreated: list[str]  # those the dump creates: dropped whole
    partitioned: list[LiveObject]  # partitioned tables the dump recreates
    absent: list[LiveObject]  # objects of those schemas the dump does not contain


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
    # Dump the privileges (GRANT/REVOKE, ALTER DEFAULT PRIVILEGES) and re-apply
    # them on restore. Off by default (--no-acl both ways): a restore then leaves
    # every recreated object with its owner's privileges only.
    keep_acl: bool = False

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


def _acl_flags(cfg: PostgresConfig) -> list[str]:
    return [] if cfg.keep_acl else ["--no-acl"]


def build_dump_argv(cfg: PostgresConfig, out_path: Path) -> list[str]:
    # Keep each excluded table's schema but drop its data (structure-only).
    exclude = [f"--exclude-table-data={t}" for t in cfg.exclude_table_data]
    if cfg.dump_format == "custom":
        return [
            "pg_dump", "--format=custom", "--compress=6",
            "--no-owner", *_acl_flags(cfg), *exclude, "--file", str(out_path),
        ]
    return ["pg_dump", "--format=plain", "--no-owner", *_acl_flags(cfg), *exclude]


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
        meta: dict[str, Any] = {"format": self.cfg.dump_format, "database": self.cfg.database}
        if self.cfg.keep_acl:
            meta["acl"] = True  # `show` tells which snapshots carry the privileges
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
    return ["--clean", "--if-exists", "--no-owner", *_acl_flags(cfg)]


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
        plan = plan_replacement(dump, env, run)
        if plan:
            _restore_replacing(cfg, dump, plan, env, run)
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


def plan_replacement(dump: Path, env: dict[str, str], run: RunFn) -> Optional[Replacement]:
    """What a restore of ``dump`` drops before the dump's own script.

    None — the plain ``pg_restore`` path — when the target is empty (a fresh
    database), or holds neither a partitioned table the dump recreates nor, in
    the snapshot's schemas, an object the dump does not contain (the snapshot's
    own schema, its data changed since).
    """
    live = run(
        ["psql", "--no-psqlrc", "--quiet", "--tuples-only", "--no-align",
         "--field-separator-zero", "--record-separator-zero",
         "--set", "ON_ERROR_STOP=1", "--command", LIVE_OBJECTS_SQL],
        env=_in_database_encoding(env), capture_output=True, timeout=CATALOG_TIMEOUT,
    )
    if live.returncode != 0:
        raise SourceError("postgres restore failed: could not list the objects "
                          f"of the target database: {_text(live.stderr)}")
    rows = [LiveObject(*row) for row in _nul_rows(live.stdout, columns=6)]
    owners = [o.name for o in rows if o.kind == "R"]
    objects = [o for o in rows if o.kind != "R"]
    if not objects:
        return None
    listing = run(["pg_restore", "--list", str(dump)],
                  env=env, capture_output=True, timeout=CATALOG_TIMEOUT)
    if listing.returncode != 0:
        raise SourceError("postgres restore failed: could not read the table of contents "
                          f"of {dump.name}: {_text(listing.stderr)}")
    keys = dump_keys(_text(listing.stdout, limit=None, errors="surrogateescape"), owners)

    relations = [o for o in objects if _OBJECT_TYPES[o.kind] in _RELATION_TYPES]
    # The snapshot's schemas are those the dump holds relations of.
    schemas = sorted(s for s in {o.schema for o in relations}
                     if any(k.startswith(f"{t} {s} ") for t in _RELATION_TYPES for k in keys))
    partitioned = [o for o in relations if o.kind == "p" and o.key() in keys]
    # Only what the snapshot's schemas hold, or a foreign key onto them, can stand
    # in the way of the dump's clean phase; a schema the dump does not hold is
    # left to itself.
    absent = [o for o in objects if o.key() not in keys
              and (o.schema in schemas or o.ref_schema in schemas)]
    if not partitioned and not absent:
        return None
    replaced = [o for o in relations if o.schema in schemas]
    if not any(o.key() in keys for o in replaced):
        # Nothing in common: another application's database, not the one the
        # snapshot was taken of. Replacing would drop all of it.
        raise SourceError(
            f"postgres restore refused: none of the {len(replaced)} relation(s) of schema(s) "
            f"{_shown(', '.join(schemas))} in the target database is in {dump.name} - is it "
            "the database the snapshot was taken of? Restore into an empty database, or drop "
            "its objects first")
    recreated = sorted(k[len("SCHEMA - "):] for k in keys
                       if k.startswith("SCHEMA - ") and k != "SCHEMA - public")
    # For the log: relations, then types, then foreign keys - none of a table
    # that is not in the snapshot either.
    gone = {(o.schema, o.name) for o in absent if o.kind != "F"}
    absent = sorted((o for o in absent if o.kind != "F" or (o.schema, o.name) not in gone),
                    key=lambda o: "TDF".find(o.kind))
    return Replacement(relations=replaced, types=[o for o in absent if o.kind in "TD"],
                       schemas=schemas, recreated=recreated, partitioned=partitioned,
                       absent=absent)


def dump_keys(listing: str, owners: Iterable[str] = ()) -> set[str]:
    """``"<type> <schema> <name>"`` of every schema, type, relation and
    constraint in a ``pg_restore --list``, its owner left off: the longest of
    ``owners`` it ends with (a role name may hold spaces), else its last word."""
    suffixes = sorted((" " + o for o in owners), key=len, reverse=True)
    keys = set()
    for line in listing.splitlines():
        match = _TOC_ENTRY.match(line)
        if match:
            rest = match.group(1)
            owner = next((s for s in suffixes if rest.endswith(s)), None)
            keys.add(rest[:-len(owner)] if owner else rest.rsplit(" ", 1)[0])
    return keys


def replacement_prelude(plan: Replacement) -> str:
    """Begin the restore's transaction and drop what ``plan`` replaces in it.

    pg_restore's own settings come first, as it applies them on connecting: no
    timeout of the role or database cuts the restore short (transaction_timeout
    exists from PostgreSQL 17 on). Each type of object goes in one statement, so
    PostgreSQL reports ("drop cascades to …") mostly the objects outside the
    replaced ones that CASCADE takes along - foreign keys, views, policies or
    defaults in other schemas or of other roles; notices are on whatever the role
    or database sets, and psql's are logged. A schema the dump creates is dropped
    whole, with whatever else the target holds in it. ``client_min_messages``
    then keeps out the warning about the script's own BEGIN, inside the
    transaction begun here; the script's SETs turn warnings back on.
    """
    lines = ["SET client_min_messages = notice;", "SET statement_timeout = 0;",
             "SET lock_timeout = 0;", "SET idle_in_transaction_session_timeout = 0;",
             "SELECT pg_catalog.set_config('transaction_timeout', '0', false) "
             "WHERE pg_catalog.current_setting('server_version_num')::int >= 170000;",
             "BEGIN;"]
    for drop in _DROP_ORDER:
        names = [o.quoted for o in [*plan.relations, *plan.types] if _OBJECT_TYPES[o.kind] == drop]
        if names:
            lines.append(f"DROP {drop} IF EXISTS {', '.join(names)} CASCADE;")
    if plan.recreated:
        quoted = ", ".join('"' + s.replace('"', '""') + '"' for s in plan.recreated)
        lines.append(f"DROP SCHEMA IF EXISTS {quoted} CASCADE;")
    lines.append("SET client_min_messages = error;")
    return "\n".join(lines) + "\n"


def _restore_replacing(cfg: PostgresConfig, dump: Path, plan: Replacement,
                       env: dict[str, str], run: RunFn) -> None:
    """Drop what ``plan`` replaces and run the dump's clean-and-create script,
    in ONE transaction: any error rolls everything back, the drops included."""
    # A file, not a pipe: if pg_restore stopped half-way, psql would commit a
    # truncated script. The script is complete before anything is executed.
    with tempfile.TemporaryDirectory(dir=dump.parent) as tmp:
        prelude = Path(tmp) / "replace.sql"
        # Names exactly as the catalog query returned them, in the database's
        # encoding: psql sends them back in the same client encoding.
        prelude.write_text(replacement_prelude(plan), encoding="utf-8", errors="surrogateescape")
        script = Path(tmp) / "restore.sql"
        # --single-transaction: the script ends with the COMMIT of the prelude's
        # transaction and has none of its own around large objects. Without it
        # pg_restore commits each batch of them - the drops and everything
        # restored before included - out of reach of a later rollback.
        generate = run(["pg_restore", *_clean_restore_flags(cfg), "--single-transaction",
                        "--file", str(script), str(dump)],
                       env=env, capture_output=True, timeout=RESTORE_TIMEOUT)
        if generate.returncode != 0:
            raise SourceError(f"postgres restore failed: {_text(generate.stderr)}")
        log.info("restoring %s by replacing the %d relation(s) of schema(s) %s "
                 "in one transaction", dump.name, len(plan.relations),
                 _shown(", ".join(plan.schemas)))
        if plan.partitioned:
            log.info("partitioned table(s) the dump recreates: %s",
                     _shown(", ".join(o.label() for o in plan.partitioned)))
        # psql stops at the first error (ON_ERROR_STOP) and the transaction still
        # open on its connection rolls back.
        apply = run(["psql", "--no-psqlrc", "--quiet", "--set", "ON_ERROR_STOP=1",
                     "--file", str(prelude), "--file", str(script)],
                    env=_in_database_encoding(env), capture_output=True, timeout=RESTORE_TIMEOUT)
        if apply.returncode != 0:
            raise SourceError(f"postgres restore failed: {_psql_error(apply.stderr)}")
        for line in _text(apply.stderr, limit=None).splitlines():
            if line.strip():
                log.info("postgres restore: %s", line.strip())
        # Only now are they gone - a rollback keeps them.
        if plan.recreated:
            log.info("schema(s) the dump creates, dropped with everything in them: %s",
                     _shown(", ".join(plan.recreated)))
        if plan.absent:
            log.info("not in the snapshot, dropped: %s",
                     _shown(", ".join(o.label() for o in plan.absent)))


def _nul_rows(data: bytes | str | None, columns: int) -> list[tuple[str, ...]]:
    """Rows of psql's unaligned output with NUL field and record separators:
    a name can hold any character but NUL, which no identifier contains. Bytes
    that are not UTF-8 (a LATIN1 database) are kept as they are."""
    raw = data.decode("utf-8", "surrogateescape") if isinstance(data, bytes) else (data or "")
    fields = raw.split("\0")
    if fields and not fields[-1].strip():
        fields.pop()  # the separator after the last record
    return [tuple(fields[i:i + columns]) for i in range(0, len(fields) - columns + 1, columns)]


def _psql_error(stderr: bytes | str | None) -> str:
    """psql's first ERROR or FATAL and what follows it (DETAIL names the object
    in the way, HINT, CONTEXT) — notices before it would crowd it out. Without
    one (a lost connection), the end of stderr."""
    lines = [line.strip() for line in _text(stderr, limit=None).splitlines()]
    first = next((i for i, line in enumerate(lines) if "ERROR:" in line or "FATAL:" in line), None)
    if first is None:
        return "\n".join(lines)[-500:]
    return "\n".join(lines[first:])[:500]


def _in_database_encoding(env: dict[str, str]) -> dict[str, str]:
    """``env`` without PGCLIENTENCODING: psql, not on a terminal, then talks in the
    database's encoding - the one ``pg_restore --list`` prints names in."""
    return {k: v for k, v in env.items() if k != "PGCLIENTENCODING"}


def _shown(text: str) -> str:
    """``text`` for the log: bytes kept by ``surrogateescape`` become U+FFFD."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _text(data: bytes | str | None, limit: int | None = 500, errors: str = "replace") -> str:
    text = data.decode("utf-8", errors) if isinstance(data, bytes) else (data or "")
    text = text.strip()
    return text if limit is None else text[:limit]


def _normalize(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Accept ``db`` as an alias for ``database`` and drop null keys."""
    out = {k: v for k, v in spec.items() if k != "type" and v is not None}
    if "database" not in out and "db" in out:
        out["database"] = out.pop("db")
    out.pop("db", None)
    return out
