"""Tests for the PostgreSQL source (argv/env builders + produce via fake run)."""

import gzip
import logging
import subprocess
from pathlib import Path

import pytest

from backuphelper.sources.base import SourceError
from backuphelper.sources.postgres import (
    LIVE_PARTITIONED_SQL,
    PostgresSource,
    build_dump_argv,
    build_env,
    build_restore_argv,
    dump_table_entries,
    partitioned_tables_to_replace,
)


def _cfg(**over):
    base = {"type": "postgres", "host": "db", "port": 5432, "database": "logto",
            "user": "logto", "password": "changeme"}
    base.update(over)
    return base


def test_env_carries_password_and_connection_but_argv_does_not():
    src = PostgresSource(_cfg())
    env = build_env(src.cfg)
    assert env["PGPASSWORD"] == "changeme"
    assert env["PGHOST"] == "db"
    assert env["PGDATABASE"] == "logto"
    argv = build_dump_argv(src.cfg, Path("/stage/database.dump"))
    assert "changeme" not in " ".join(argv)  # password never on the command line


def test_custom_format_argv():
    src = PostgresSource(_cfg(dump_format="custom"))
    out = Path("/stage/database.dump")
    argv = build_dump_argv(src.cfg, out)
    assert "--format=custom" in argv
    assert "--no-owner" in argv and "--no-acl" in argv
    assert argv[-2:] == ["--file", str(out)]


def test_plain_format_argv():
    src = PostgresSource(_cfg(dump_format="plain"))
    argv = build_dump_argv(src.cfg, Path("/stage/database.sql.gz"))
    assert "--format=plain" in argv


def test_accepts_db_alias_for_database():
    src = PostgresSource(_cfg(database=None, db="mydb"))
    assert src.cfg.database == "mydb"


class _FakeRun:
    def __init__(self, rc=0, stderr=b""):
        self.rc = rc
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        # custom format writes to the --file target
        if "--file" in argv:
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDUMPDATA")
        stdout = b"SELECT 1;\n"
        return subprocess.CompletedProcess(argv, self.rc, stdout, self.stderr)


def test_produce_custom_stages_dump_file(tmp_path):
    run = _FakeRun()
    src = PostgresSource(_cfg(dump_format="custom"), run=run)
    comps = src.produce(tmp_path)
    assert len(comps) == 1
    c = comps[0]
    assert c.kind == "postgres" and c.error is None
    assert c.path is not None and c.path.exists()
    assert c.metadata["format"] == "custom"


def test_produce_plain_writes_gzip(tmp_path):
    run = _FakeRun()
    src = PostgresSource(_cfg(dump_format="plain"), run=run)
    comps = src.produce(tmp_path)
    c = comps[0]
    assert c.path.suffix == ".gz"
    assert gzip.decompress(c.path.read_bytes()) == b"SELECT 1;\n"


def test_produce_failure_returns_errored_component(tmp_path):
    run = _FakeRun(rc=1, stderr=b"connection refused")
    src = PostgresSource(_cfg(), run=run)
    comps = src.produce(tmp_path)
    assert comps[0].error is not None
    assert "connection refused" in comps[0].error
    assert comps[0].path is None


def test_restore_argv_for_custom_dump():
    from backuphelper.sources.postgres import build_restore_argv
    argv = build_restore_argv(PostgresSource(_cfg()).cfg, Path("/r/database.dump"))
    assert argv[0] == "pg_restore"
    assert "--clean" in argv and "--if-exists" in argv and "--single-transaction" in argv
    assert argv[-1] == str(Path("/r/database.dump"))


def test_restore_argv_for_plain_sql():
    from backuphelper.sources.postgres import build_restore_argv
    argv = build_restore_argv(PostgresSource(_cfg()).cfg, Path("/r/database.sql"))
    assert argv[0] == "psql"


def test_dump_argv_excludes_table_data_both_formats():
    # Keep a table's STRUCTURE but drop its row DATA (truncate effect) — used to
    # strip n8n execution history from the dump while preserving the schema.
    from backuphelper.sources.postgres import PostgresConfig, build_dump_argv
    for fmt, dump in (("custom", "/x/db.dump"), ("plain", "/x/db.sql")):
        cfg = PostgresConfig(dump_format=fmt,
                             exclude_table_data=["execution_entity", "execution_data"])
        argv = build_dump_argv(cfg, Path(dump))
        assert "--exclude-table-data=execution_entity" in argv
        assert "--exclude-table-data=execution_data" in argv


def test_exclude_table_data_accepts_csv():
    from backuphelper.sources.postgres import PostgresConfig
    assert PostgresConfig(exclude_table_data="execution_entity, execution_data").exclude_table_data == [
        "execution_entity", "execution_data"]
    assert PostgresConfig().exclude_table_data == []


def test_plain_restore_stops_on_first_error():
    # A plain psql restore must fail loudly (ON_ERROR_STOP=1) instead of swallowing
    # per-statement errors and exiting 0 — otherwise a restore onto a non-empty DB
    # reports success while restoring nothing.
    from backuphelper.sources.postgres import build_restore_argv
    for dump in (Path("/r/database.sql.gz"), Path("/r/database.sql")):
        argv = build_restore_argv(PostgresSource(_cfg()).cfg, dump)
        assert argv[0] == "psql"
        assert "--set" in argv and "ON_ERROR_STOP=1" in argv, argv


def test_restore_runs_pg_restore_for_dump(tmp_path):
    # component name defaults to the database name ("logto"); the target has no
    # partitioned tables, so the catalog query is followed by plain pg_restore.
    (tmp_path / "logto.dump").write_bytes(b"x")
    run = _FakeRun()
    PostgresSource(_cfg(), run=run).restore(tmp_path)
    assert run.calls and run.calls[-1][0] == "pg_restore"


def test_component_name_defaults_to_database_name():
    assert PostgresSource(_cfg(database="mydb")).cfg.component_name() == "mydb"


# --- partition-safe restore -------------------------------------------------
# pg_restore --clean drops each partition's primary key on its own, which
# PostgreSQL refuses ("cannot drop inherited constraint"), so a restore over a
# database that holds partitioned tables rolled back completely.

TOC = """\
;
; Archive created at 2026-10-08 11:46:35 UTC
;     dbname: app
;
; Selected TOC Entries:
;
6; 2615 16390 SCHEMA - cache app
240; 1259 16500 TABLE cache objects app
241; 1259 16510 TABLE cache objects_p1 app
242; 1259 16520 TABLE public my events app
243; 1259 16530 TABLE public plain app
250; 1259 16540 VIEW public objects_view app
3601; 0 0 TABLE ATTACH cache objects_p1 app
3700; 0 16540 TABLE DATA public plain app
3800; 2606 16600 CONSTRAINT cache objects objects_pkey app
3801; 0 0 INDEX ATTACH cache objects_p1_pkey app
"""

# psql --no-align --tuples-only with NUL field and record separators.
LIVE = "\0".join(["cache", "objects", "cache.objects",
                  "other", "live_only", "other.live_only",
                  "public", "my events", 'public."my events"']) + "\0"


class _PgRun:
    """Answers each command of a postgres restore; records argv, env and the SQL psql applies."""

    def __init__(self, live="", live_rc=0, list_rc=0, generate_rc=0, apply_rc=0,
                 apply_stderr=""):
        self.live, self.live_rc, self.list_rc = live, live_rc, list_rc
        self.generate_rc, self.apply_rc, self.apply_stderr = generate_rc, apply_rc, apply_stderr
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.applied_sql: list[str] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.envs.append(kw.get("env") or {})
        if argv[0] == "psql" and "--command" in argv:
            return self._done(argv, self.live_rc, self.live, "psql: connection refused")
        if argv[:2] == ["pg_restore", "--list"]:
            return self._done(argv, self.list_rc, TOC, "pg_restore: not a valid archive")
        if argv[0] == "pg_restore" and "--file" in argv:
            Path(argv[argv.index("--file") + 1]).write_text("-- dump script\n", encoding="utf-8")
            return self._done(argv, self.generate_rc, "", "pg_restore: corrupt archive")
        if argv[0] == "psql" and "--single-transaction" in argv:
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "--file"]
            self.applied_sql.append("".join(Path(f).read_text(encoding="utf-8") for f in files))
            stderr = self.apply_stderr if self.apply_rc == 0 else "ERROR:  something failed"
            return subprocess.CompletedProcess(argv, self.apply_rc, b"", stderr.encode())
        return self._done(argv, 0, "", "")  # plain pg_restore / psql

    @staticmethod
    def _done(argv, rc, stdout, stderr):
        return subprocess.CompletedProcess(argv, rc, stdout.encode(), stderr.encode() if rc else b"")


def _staged_dump(tmp_path, name="logto.dump"):
    (tmp_path / name).write_bytes(b"PGDMP")
    return tmp_path


def test_dump_table_entries_lists_tables_only():
    assert dump_table_entries(TOC) == [
        "cache objects app ", "cache objects_p1 app ", "public my events app ",
        "public plain app ",
    ]


def test_only_live_partitioned_tables_the_dump_recreates_are_replaced(tmp_path):
    run = _PgRun(live=LIVE)
    dump = _staged_dump(tmp_path) / "logto.dump"
    tables = partitioned_tables_to_replace(dump, {}, run)
    # other.live_only is not in the dump: a restore never touches it.
    assert tables == ["cache.objects", 'public."my events"']
    assert run.calls[0][run.calls[0].index("--command") + 1] == LIVE_PARTITIONED_SQL
    assert "--field-separator-zero" in run.calls[0] and "--record-separator-zero" in run.calls[0]


def test_without_live_partitioned_tables_the_plain_restore_runs(tmp_path):
    run = _PgRun(live="")
    src = PostgresSource(_cfg(), run=run)
    src.restore(_staged_dump(tmp_path))
    # No TOC read, no script: the catalog query, then exactly the old pg_restore.
    assert len(run.calls) == 2
    assert run.calls[-1] == build_restore_argv(src.cfg, tmp_path / "logto.dump")
    assert run.applied_sql == []


def test_partitioned_tables_are_dropped_in_the_restore_transaction(tmp_path):
    run = _PgRun(live=LIVE)
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))

    generate = next(c for c in run.calls if c[0] == "pg_restore" and "--file" in c)
    assert {"--clean", "--if-exists", "--no-owner", "--no-acl"} <= set(generate)
    assert "--single-transaction" not in generate  # psql owns the transaction
    apply = run.calls[-1]
    assert apply[0] == "psql" and "--single-transaction" in apply
    assert apply[apply.index("--set") + 1] == "ON_ERROR_STOP=1"
    assert "--no-psqlrc" in apply
    # The drops come first, then the dump's own clean-and-create script.
    assert run.applied_sql == [
        "DROP TABLE IF EXISTS cache.objects CASCADE;\n"
        'DROP TABLE IF EXISTS public."my events" CASCADE;\n'
        "-- dump script\n"
    ]


def test_psql_notices_of_the_replacement_are_logged(tmp_path, caplog):
    run = _PgRun(live=LIVE, apply_stderr="NOTICE:  drop cascades to view public.objects_view\n")
    with caplog.at_level(logging.INFO, logger="backuphelper.sources.postgres"):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    messages = [r.getMessage() for r in caplog.records]
    assert any("2 partitioned table(s)" in m and "cache.objects" in m for m in messages)
    assert any("drop cascades to view public.objects_view" in m for m in messages)


def test_replacement_never_puts_the_password_on_a_command_line(tmp_path):
    run = _PgRun(live=LIVE)
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert all("changeme" not in " ".join(argv) for argv in run.calls)
    assert all(env.get("PGPASSWORD") == "changeme" for env in run.envs)


def test_replacement_removes_its_temporary_files(tmp_path):
    run = _PgRun(live=LIVE)
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["logto.dump"]


@pytest.mark.parametrize("failure", [
    {"live_rc": 2}, {"list_rc": 1}, {"generate_rc": 1}, {"apply_rc": 3},
])
def test_every_failing_step_fails_the_restore(tmp_path, failure):
    run = _PgRun(live=LIVE, **failure)
    with pytest.raises(SourceError, match="postgres restore failed"):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))


def test_a_failed_replacement_reports_the_error_not_the_notices(tmp_path):
    # The prelude's notices precede the error in stderr; 40 of them would fill
    # the 500-character message before the ERROR line.
    notices = "".join(f"NOTICE:  drop cascades to constraint fk_{i} on table t{i}\n"
                      for i in range(40))
    run = _PgRun(live=LIVE)

    def failing_apply(argv, **kw):
        result = run(argv, **kw)
        if argv[0] == "psql" and "--single-transaction" in argv:
            stderr = notices + 'psql:restore.sql:9: ERROR:  relation "x" already exists\n'
            return subprocess.CompletedProcess(argv, 3, b"", stderr.encode())
        return result

    with pytest.raises(SourceError) as err:
        PostgresSource(_cfg(), run=failing_apply).restore(_staged_dump(tmp_path))
    assert 'ERROR:  relation "x" already exists' in str(err.value)
    assert "drop cascades" not in str(err.value)


def test_a_failed_script_generation_never_reaches_the_database(tmp_path):
    run = _PgRun(live=LIVE, generate_rc=1)
    with pytest.raises(SourceError):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert run.applied_sql == []


def test_privileges_are_dropped_by_default_both_ways(tmp_path):
    cfg = PostgresSource(_cfg()).cfg
    assert cfg.keep_acl is False
    for fmt, dump in (("custom", "/x/db.dump"), ("plain", "/x/db.sql")):
        assert "--no-acl" in build_dump_argv(PostgresSource(_cfg(dump_format=fmt)).cfg, Path(dump))
    assert "--no-acl" in build_restore_argv(cfg, Path("/r/logto.dump"))
    comp = PostgresSource(_cfg(), run=_FakeRun()).produce(tmp_path)[0]
    assert "acl" not in comp.metadata


def test_keep_acl_dumps_and_restores_the_privileges(tmp_path):
    # A restricted runtime role keeps its GRANTs across a restore (CS-IAMStack
    # needed a post_restore hook for that while the engine forced --no-acl).
    for fmt, dump in (("custom", "/x/db.dump"), ("plain", "/x/db.sql")):
        cfg = PostgresSource(_cfg(dump_format=fmt, keep_acl=True)).cfg
        argv = build_dump_argv(cfg, Path(dump))
        assert "--no-acl" not in argv and "--no-owner" in argv
    cfg = PostgresSource(_cfg(keep_acl=True)).cfg
    argv = build_restore_argv(cfg, Path("/r/logto.dump"))
    assert "--no-acl" not in argv and "--no-owner" in argv and "--clean" in argv
    comp = PostgresSource(_cfg(keep_acl=True), run=_FakeRun()).produce(tmp_path)[0]
    assert comp.metadata["acl"] is True


def test_keep_acl_applies_to_the_partition_safe_restore(tmp_path):
    run = _PgRun(live=LIVE)
    PostgresSource(_cfg(keep_acl=True), run=run).restore(_staged_dump(tmp_path))
    generate = next(c for c in run.calls if c[0] == "pg_restore" and "--file" in c)
    assert "--no-acl" not in generate and "--clean" in generate


def test_keep_acl_accepts_an_interpolated_string():
    assert PostgresSource(_cfg(keep_acl="true")).cfg.keep_acl is True


def test_plain_dumps_skip_the_catalog_query(tmp_path):
    (tmp_path / "logto.sql.gz").write_bytes(gzip.compress(b"SELECT 1;"))
    run = _PgRun(live=LIVE)
    PostgresSource(_cfg(dump_format="plain"), run=run).restore(tmp_path)
    assert all("--command" not in c for c in run.calls)
    assert len(run.calls) == 1 and run.calls[0][0] == "psql"
