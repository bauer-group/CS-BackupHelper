"""Tests for the PostgreSQL source (argv/env builders + produce via fake run)."""

import gzip
import subprocess
from pathlib import Path

from backuphelper.sources.postgres import PostgresSource, build_dump_argv, build_env


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
    # component name defaults to the database name ("logto")
    (tmp_path / "logto.dump").write_bytes(b"x")
    run = _FakeRun()
    PostgresSource(_cfg(), run=run).restore(tmp_path)
    assert run.calls and run.calls[0][0] == "pg_restore"


def test_component_name_defaults_to_database_name():
    assert PostgresSource(_cfg(database="mydb")).cfg.component_name() == "mydb"
