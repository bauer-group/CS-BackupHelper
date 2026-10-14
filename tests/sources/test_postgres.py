"""Tests for the PostgreSQL source (argv/env builders + produce via fake run)."""

import gzip
import logging
import subprocess
from pathlib import Path

import pytest

from backuphelper.sources.base import SourceError
from backuphelper.sources.postgres import (
    LIVE_OBJECTS_SQL,
    LiveObject,
    PostgresSource,
    Replacement,
    build_dump_argv,
    build_env,
    build_restore_argv,
    dump_keys,
    partitioned_tables_to_replace,
    plan_replacement,
    replacement_prelude,
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
    # component name defaults to the database name ("logto"); the target is
    # empty, so the catalog query is followed by plain pg_restore.
    (tmp_path / "logto.dump").write_bytes(b"x")
    run = _FakeRun()
    PostgresSource(_cfg(), run=run).restore(tmp_path)
    assert run.calls and run.calls[-1][0] == "pg_restore"


def test_component_name_defaults_to_database_name():
    assert PostgresSource(_cfg(database="mydb")).cfg.component_name() == "mydb"


# --- restoring over a target pg_restore --clean cannot clean ----------------
# pg_restore --clean drops only the dump's own objects. Over partitioned tables
# it drops each partition's primary key on its own ("cannot drop inherited
# constraint"); over a schema a newer application version migrated, an object
# the dump does not know blocks the drop of one it does ("cannot drop index
# pk_workflow_entity_id because other objects depend on it" - n8n's
# workflow_suggestion). Either way the restore rolled back completely.

TOC = """\
;
; Archive created at 2026-10-08 11:46:35 UTC
;     dbname: app
;
; Selected TOC Entries:
;
6; 2615 16390 SCHEMA - cache app
7; 2615 2200 SCHEMA - public pg_database_owner
230; 1247 16400 TYPE public mood app
231; 1247 16401 DOMAIN public posint app
240; 1259 16500 TABLE cache objects app
241; 1259 16510 TABLE cache objects_p1 app
242; 1259 16520 TABLE public my events app
243; 1259 16530 TABLE public plain app
244; 1259 16535 SEQUENCE public counter app
245; 1259 16536 VIEW public workflow history app
250; 1259 16540 VIEW public objects_view app
3601; 0 0 TABLE ATTACH cache objects_p1 app
3700; 0 16540 TABLE DATA public plain app
3800; 2606 16600 CONSTRAINT cache objects objects_pkey app
3801; 0 0 INDEX ATTACH cache objects_p1_pkey app
3802; 1259 16610 INDEX public plain_name_idx app
3803; 2606 16620 FK CONSTRAINT public plain plain_ref_fkey app
"""


def _o(kind, schema, name, quoted=None, constraint="", ref=""):
    """One row of LIVE_OBJECTS_SQL."""
    return (kind, schema, name, constraint, quoted or f"{schema}.{name}", ref)


def _live(*rows):
    """psql --no-align --tuples-only output of LIVE_OBJECTS_SQL, NUL-separated."""
    return "".join("\0".join(row) + "\0" for row in rows)


PLAIN = _o("r", "public", "plain")
# The snapshot's own schema, its data changed since: nothing to replace.
LIVE_SAME = _live(PLAIN, _o("S", "public", "counter"), _o("v", "public", "objects_view"),
                  _o("v", "public", "workflow history", 'public."workflow history"'),
                  _o("T", "public", "mood"), _o("D", "public", "posint"),
                  _o("F", "public", "plain", constraint="plain_ref_fkey", ref="public"))
# Partitioned tables the dump recreates - and nothing it does not contain.
LIVE_PARTITIONED = _live(_o("p", "cache", "objects"),
                         _o("p", "public", "my events", 'public."my events"'), PLAIN)
# A newer application version migrated the target: a table, a view, a type and
# foreign keys the snapshot does not contain.
LIVE_MIGRATED = _live(PLAIN, _o("r", "public", "suggestion"), _o("v", "public", "suggestion_view"),
                      _o("T", "public", "suggestion_kind"),
                      _o("F", "public", "suggestion", constraint="suggestion_plain_fkey",
                         ref="public"),
                      _o("F", "public", "plain", constraint="plain_new_fkey", ref="public"))


class _PgRun:
    """Answers each command of a postgres restore; records argv, env and the SQL psql applies."""

    def __init__(self, live="", live_rc=0, list_rc=0, generate_rc=0, apply_rc=0,
                 apply_stderr="", toc=None):
        self.live, self.live_rc, self.list_rc = live, live_rc, list_rc
        self.toc = TOC.encode() if toc is None else toc
        self.generate_rc, self.apply_rc, self.apply_stderr = generate_rc, apply_rc, apply_stderr
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.applied_sql: list[str] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.envs.append(kw.get("env") or {})
        if argv[0] == "psql" and "--command" in argv:
            live = self.live.encode("utf-8", "surrogateescape")
            return self._done(argv, self.live_rc, live, "psql: connection refused")
        if argv[:2] == ["pg_restore", "--list"]:
            return self._done(argv, self.list_rc, self.toc, "pg_restore: not a valid archive")
        if argv[0] == "pg_restore" and "--file" in argv:
            Path(argv[argv.index("--file") + 1]).write_text("-- dump script\n", encoding="utf-8")
            return self._done(argv, self.generate_rc, b"", "pg_restore: corrupt archive")
        if _is_apply(argv):
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "--file"]
            self.applied_sql.append("".join(
                Path(f).read_text(encoding="utf-8", errors="surrogateescape") for f in files))
            stderr = self.apply_stderr if self.apply_rc == 0 else "ERROR:  something failed"
            return subprocess.CompletedProcess(argv, self.apply_rc, b"", stderr.encode())
        return self._done(argv, 0, b"", "")  # plain pg_restore / psql

    @staticmethod
    def _done(argv, rc, stdout, stderr):
        return subprocess.CompletedProcess(argv, rc, stdout, stderr.encode() if rc else b"")


def _is_apply(argv):
    """The psql that applies the prelude and the dump's script."""
    return argv[0] == "psql" and "--no-psqlrc" in argv and "--file" in argv


def _staged_dump(tmp_path, name="logto.dump"):
    (tmp_path / name).write_bytes(b"PGDMP")
    return tmp_path


def _plan(tmp_path, live, toc=None):
    return plan_replacement(_staged_dump(tmp_path) / "logto.dump", {}, _PgRun(live=live, toc=toc))


def _replaced(plan):
    return [o.quoted for o in plan.relations]


def _absent(plan):
    return [o.label() for o in plan.absent]


def test_dump_keys_list_schemas_types_relations_and_constraints_without_owner():
    assert dump_keys(TOC) == {
        "SCHEMA - cache", "SCHEMA - public", "TYPE public mood", "DOMAIN public posint",
        "TABLE cache objects", "TABLE cache objects_p1", "TABLE public my events",
        "TABLE public plain", "SEQUENCE public counter", "VIEW public workflow history",
        "VIEW public objects_view", "CONSTRAINT cache objects objects_pkey",
        "INDEX public plain_name_idx", "FK CONSTRAINT public plain plain_ref_fkey",
    }


def test_the_catalog_query_reads_nul_separated_rows(tmp_path):
    run = _PgRun(live=LIVE_SAME)
    plan_replacement(_staged_dump(tmp_path) / "logto.dump", {}, run)
    query = run.calls[0]
    assert query[query.index("--command") + 1] == LIVE_OBJECTS_SQL
    assert "--field-separator-zero" in query and "--record-separator-zero" in query


def test_the_catalog_query_leaves_out_what_a_restore_must_not_drop():
    # Partitions (their parent takes them along), extension members - relations,
    # the keys between them and types -, a column's own sequence, objects of
    # another owner, a partition's copy of a key, a table's row type.
    for clause in ("NOT c.relispartition", "d.deptype = 'e' OR", "d.deptype IN ('a', 'i')",
                   "pg_has_role(c.relowner, 'USAGE')", "con.conparentid = 0",
                   "d.objid = c.oid AND d.deptype = 'e'", "pg_has_role(t.typowner, 'USAGE')",
                   "d.objid = t.oid AND d.deptype = 'e'", "c.relkind = 'c'",
                   "n.nspname !~ '^pg_'", "information_schema"):
        assert clause in LIVE_OBJECTS_SQL, clause


def test_an_empty_target_takes_the_plain_restore(tmp_path):
    run = _PgRun(live=_live(("R", "", "app", "", "", "")))  # only the cluster's roles
    src = PostgresSource(_cfg(), run=run)
    src.restore(_staged_dump(tmp_path))
    # No TOC read, no script: the catalog query, then exactly the old pg_restore.
    assert len(run.calls) == 2
    assert run.calls[-1] == build_restore_argv(src.cfg, tmp_path / "logto.dump")
    assert run.applied_sql == []


def test_the_snapshots_own_schema_takes_the_plain_restore(tmp_path):
    run = _PgRun(live=LIVE_SAME)
    src = PostgresSource(_cfg(), run=run)
    src.restore(_staged_dump(tmp_path))
    assert run.calls[1][:2] == ["pg_restore", "--list"]
    assert run.calls[-1] == build_restore_argv(src.cfg, tmp_path / "logto.dump")
    assert run.applied_sql == []


def test_a_partitioned_table_the_dump_recreates_is_enough(tmp_path):
    plan = _plan(tmp_path, LIVE_PARTITIONED)
    assert [o.quoted for o in plan.partitioned] == ["cache.objects", 'public."my events"']
    assert plan.schemas == ["cache", "public"]
    assert _replaced(plan) == ["cache.objects", 'public."my events"', "public.plain"]
    assert plan.absent == [] and plan.types == []


def test_the_1_9_interface_still_names_the_partitioned_tables(tmp_path):
    # Consumers probe for it: CS-IAM's image build fails without it.
    dump = _staged_dump(tmp_path) / "logto.dump"
    assert partitioned_tables_to_replace(dump, {}, _PgRun(live=LIVE_PARTITIONED)) == [
        "cache.objects", 'public."my events"']
    assert partitioned_tables_to_replace(dump, {}, _PgRun(live=LIVE_SAME)) == []
    assert partitioned_tables_to_replace(dump, {}, _PgRun(live=LIVE_MIGRATED)) == []


def test_objects_the_snapshot_does_not_contain_replace_the_snapshots_schemas(tmp_path):
    plan = _plan(tmp_path, LIVE_MIGRATED)
    assert plan.partitioned == []
    assert plan.schemas == ["public"]
    assert _replaced(plan) == ["public.plain", "public.suggestion", "public.suggestion_view"]
    assert [o.quoted for o in plan.types] == ["public.suggestion_kind"]
    # Relations, types, then keys; suggestion's own key goes with it, unlisted.
    assert _absent(plan) == ["public.suggestion", "public.suggestion_view",
                             "type public.suggestion_kind",
                             "foreign key plain_new_fkey on public.plain"]


def test_a_foreign_key_the_snapshot_does_not_contain_is_enough(tmp_path):
    # A migration that only links two of the snapshot's tables: the key holds on
    # to the referenced table's index, which the dump drops.
    plan = _plan(tmp_path, _live(PLAIN, _o("F", "public", "plain", constraint="plain_new_fkey",
                                           ref="public")))
    assert _replaced(plan) == ["public.plain"]
    assert _absent(plan) == ["foreign key plain_new_fkey on public.plain"]


def test_a_foreign_key_from_another_schema_onto_the_snapshot_is_enough(tmp_path):
    plan = _plan(tmp_path, _live(PLAIN, _o("r", "reporting", "kpi"),
                                 _o("F", "reporting", "kpi", constraint="kpi_plain_fkey",
                                    ref="public")))
    assert _replaced(plan) == ["public.plain"]  # reporting.kpi stays, its key goes
    assert _absent(plan) == ["foreign key kpi_plain_fkey on reporting.kpi"]


def test_objects_outside_the_snapshots_schemas_change_nothing(tmp_path):
    # A schema the dump does not hold, with nothing pointing into the snapshot's.
    assert _plan(tmp_path, _live(PLAIN, _o("r", "audit", "log"),
                                 _o("F", "audit", "log", constraint="log_parent_fkey",
                                    ref="audit"))) is None


def test_a_relation_of_another_type_under_a_dumped_name_is_not_in_the_dump(tmp_path):
    # The dump's own DROP VIEW would fail on a table: "objects_view" is not a view.
    plan = _plan(tmp_path, _live(PLAIN, _o("r", "public", "objects_view")))
    assert _absent(plan) == ["public.objects_view"]


def test_a_name_another_one_begins_with_is_not_in_the_dump(tmp_path):
    # "VIEW public workflow" is not "VIEW public workflow history".
    plan = _plan(tmp_path, _live(PLAIN, _o("v", "public", "workflow")))
    assert _absent(plan) == ["public.workflow"]


def test_the_dump_creates_its_schemas_but_never_public(tmp_path):
    plan = _plan(tmp_path, LIVE_MIGRATED)
    assert plan.recreated == ["cache"]


def test_the_prelude_drops_each_type_of_object_in_one_statement(tmp_path):
    sql = replacement_prelude(_plan(tmp_path, LIVE_MIGRATED))
    assert sql.splitlines() == [
        # notices on, whatever the role sets: they name what CASCADE takes along
        "SET client_min_messages = notice;",
        # pg_restore's own settings: no timeout of the role cuts the drops short
        "SET statement_timeout = 0;",
        "SET lock_timeout = 0;",
        "SET idle_in_transaction_session_timeout = 0;",
        "SELECT pg_catalog.set_config('transaction_timeout', '0', false) "
        "WHERE pg_catalog.current_setting('server_version_num')::int >= 170000;",
        "BEGIN;",
        "DROP VIEW IF EXISTS public.suggestion_view CASCADE;",
        "DROP TABLE IF EXISTS public.plain, public.suggestion CASCADE;",
        "DROP TYPE IF EXISTS public.suggestion_kind CASCADE;",
        'DROP SCHEMA IF EXISTS "cache" CASCADE;',
        # the script's own BEGIN warns inside this transaction
        "SET client_min_messages = error;",
    ]


def test_the_prelude_drops_what_depends_on_others_first():
    objects = [LiveObject(k, "public", n, "", f"public.{n}", "")
               for k, n in [("S", "s"), ("r", "t"), ("p", "pt"), ("f", "ft"), ("v", "v"),
                            ("m", "mv"), ("D", "d"), ("T", "e")]]
    plan = Replacement(relations=objects[:6], types=objects[6:], schemas=["public"],
                       recreated=['we"ird'], partitioned=[], absent=[])
    assert replacement_prelude(plan).splitlines()[6:] == [
        "DROP MATERIALIZED VIEW IF EXISTS public.mv CASCADE;",
        "DROP VIEW IF EXISTS public.v CASCADE;",
        "DROP FOREIGN TABLE IF EXISTS public.ft CASCADE;",
        "DROP TABLE IF EXISTS public.t, public.pt CASCADE;",
        "DROP SEQUENCE IF EXISTS public.s CASCADE;",
        "DROP DOMAIN IF EXISTS public.d CASCADE;",
        "DROP TYPE IF EXISTS public.e CASCADE;",
        'DROP SCHEMA IF EXISTS "we""ird" CASCADE;',
        "SET client_min_messages = error;",
    ]


def test_the_relations_are_dropped_in_the_restore_transaction(tmp_path):
    run = _PgRun(live=LIVE_MIGRATED)
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))

    generate = next(c for c in run.calls if c[0] == "pg_restore" and "--file" in c)
    assert {"--clean", "--if-exists", "--no-owner", "--no-acl"} <= set(generate)
    # One BEGIN/COMMIT for the whole script, none of its own around large objects.
    assert "--single-transaction" in generate
    apply = run.calls[-1]
    assert apply[0] == "psql" and "--single-transaction" not in apply  # the prelude begins it
    assert apply[apply.index("--set") + 1] == "ON_ERROR_STOP=1"
    assert "--no-psqlrc" in apply
    # The prelude first, then the dump's own clean-and-create script.
    assert len(run.applied_sql) == 1
    prelude, script = run.applied_sql[0].split("SET client_min_messages = error;\n")
    assert prelude.startswith("SET client_min_messages = notice;\n") and "BEGIN;\n" in prelude
    assert script == "-- dump script\n"


def test_types_outside_the_snapshots_schemas_are_never_dropped(tmp_path):
    # Dropping reporting.grade would take reporting's columns of it along.
    plan = _plan(tmp_path, LIVE_MIGRATED + _live(_o("T", "reporting", "grade"),
                                                 _o("D", "reporting", "pct")))
    assert [o.quoted for o in plan.types] == ["public.suggestion_kind"]
    assert "reporting" not in replacement_prelude(plan)


def test_a_target_sharing_nothing_with_the_snapshot_is_refused(tmp_path):
    # Another application's database: replacing would drop all of it.
    run = _PgRun(live=_live(_o("r", "public", "users"), _o("r", "public", "orders")))
    with pytest.raises(SourceError, match="refused: none of the 2 relation"):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert run.applied_sql == [] and not any("--file" in c for c in run.calls)


def test_an_owner_whose_name_holds_spaces_is_left_off_whole(tmp_path):
    # An Entra ID group as owner, as on Azure: "App Owner", not "Owner".
    toc = TOC.replace(" app\n", " App Owner\n").encode()
    assert "TABLE public plain" in dump_keys(toc.decode(), ["App Owner", "app"])
    roles = _live(("R", "", "App Owner", "", "", ""), ("R", "", "app", "", "", ""))
    assert _plan(tmp_path, roles + LIVE_SAME, toc=toc) is None
    assert _plan(tmp_path, roles + LIVE_MIGRATED, toc=toc).recreated == ["cache"]


def test_names_in_another_encoding_reach_the_drop_unchanged(tmp_path):
    # A LATIN1 database: psql returns "prüfung" as the byte 0xE4, which the
    # prelude must send back as it came - not as U+FFFD.
    name = b"pr\xe4fung".decode("utf-8", "surrogateescape")
    run = _PgRun(live=_live(PLAIN, _o("r", "public", name)))
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert f"public.{name}" in run.applied_sql[0]


def test_names_in_another_encoding_match_the_dump_and_are_logged_readably(tmp_path, caplog):
    # pg_restore --list prints the dump's LATIN1 bytes as they are.
    toc = TOC.encode() + b"260; 1259 16700 TABLE public pr\xe4fung app\n"
    name = b"pr\xe4fung".decode("utf-8", "surrogateescape")
    assert _plan(tmp_path, _live(PLAIN, _o("r", "public", name)), toc=toc) is None
    other = b"\xfcbrig".decode("utf-8", "surrogateescape")
    run = _PgRun(live=_live(PLAIN, _o("r", "public", name), _o("r", "public", other)), toc=toc)
    with caplog.at_level(logging.INFO, logger="backuphelper.sources.postgres"):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert "not in the snapshot, dropped: public.�brig" in [r.getMessage() for r in caplog.records]


def test_the_catalog_query_and_the_restore_talk_in_the_database_encoding(tmp_path):
    run = _PgRun(live=LIVE_MIGRATED)
    plan = plan_replacement(_staged_dump(tmp_path) / "logto.dump",
                            {"PGCLIENTENCODING": "UTF8", "PGPASSWORD": "x"}, run)
    assert "PGCLIENTENCODING" not in run.envs[0] and run.envs[0]["PGPASSWORD"] == "x"
    assert plan is not None


def test_the_replacement_is_logged(tmp_path, caplog):
    stderr = ("psql:replace.sql:6: NOTICE:  drop cascades to view reporting.kpi_view\n"
              "WARNING:  something to see\n")
    run = _PgRun(live=LIVE_MIGRATED, apply_stderr=stderr)
    with caplog.at_level(logging.INFO, logger="backuphelper.sources.postgres"):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    messages = [r.getMessage() for r in caplog.records]
    assert any("replacing the 3 relation(s) of schema(s) " in m for m in messages)
    assert "schema(s) the dump creates, dropped with everything in them: cache" in messages
    assert ("not in the snapshot, dropped: public.suggestion, public.suggestion_view, "
            "type public.suggestion_kind, foreign key plain_new_fkey on public.plain") in messages
    # What CASCADE took along outside the replaced objects is in the log.
    assert any("drop cascades to view reporting.kpi_view" in m for m in messages)
    assert any("WARNING:  something to see" in m for m in messages)


def test_what_was_dropped_is_logged_only_once_it_is(tmp_path, caplog):
    # A rollback keeps every object: no line may claim it was dropped.
    run = _PgRun(live=LIVE_MIGRATED, apply_rc=3)
    with caplog.at_level(logging.INFO, logger="backuphelper.sources.postgres"):
        with pytest.raises(SourceError):
            PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    messages = [r.getMessage() for r in caplog.records]
    assert any("by replacing" in m for m in messages)
    assert not any("dropped" in m for m in messages)


def test_partitioned_tables_are_logged(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="backuphelper.sources.postgres"):
        PostgresSource(_cfg(), run=_PgRun(live=LIVE_PARTITIONED)).restore(_staged_dump(tmp_path))
    messages = [r.getMessage() for r in caplog.records]
    assert 'partitioned table(s) the dump recreates: cache.objects, public."my events"' in messages
    assert not any(m.startswith("not in the snapshot") for m in messages)


def test_replacement_never_puts_the_password_on_a_command_line(tmp_path):
    run = _PgRun(live=LIVE_MIGRATED)
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert all("changeme" not in " ".join(argv) for argv in run.calls)
    assert all(env.get("PGPASSWORD") == "changeme" for env in run.envs)


def test_replacement_removes_its_temporary_files(tmp_path):
    run = _PgRun(live=LIVE_MIGRATED)
    PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["logto.dump"]


@pytest.mark.parametrize("failure", [
    {"live_rc": 2}, {"list_rc": 1}, {"generate_rc": 1}, {"apply_rc": 3},
])
def test_every_failing_step_fails_the_restore(tmp_path, failure):
    run = _PgRun(live=LIVE_MIGRATED, **failure)
    with pytest.raises(SourceError, match="postgres restore failed"):
        PostgresSource(_cfg(), run=run).restore(_staged_dump(tmp_path))


def test_a_failed_replacement_reports_the_error_with_its_detail(tmp_path):
    # Notices before the error would fill the 500-character message before the
    # ERROR line; the DETAIL after it names the object in the way.
    notices = "".join(f"NOTICE:  drop cascades to constraint fk_{i} on table t{i}\n"
                      for i in range(40))
    error = ("psql:restore.sql:9: ERROR:  cannot drop type public.mood because other objects "
             "depend on it\nDETAIL:  function public.is_ok(public.mood) depends on type "
             "public.mood\n")
    run = _PgRun(live=LIVE_MIGRATED)

    def failing_apply(argv, **kw):
        result = run(argv, **kw)
        if _is_apply(argv):
            return subprocess.CompletedProcess(argv, 3, b"", (notices + error).encode())
        return result

    with pytest.raises(SourceError) as err:
        PostgresSource(_cfg(), run=failing_apply).restore(_staged_dump(tmp_path))
    assert "ERROR:  cannot drop type public.mood" in str(err.value)
    assert "DETAIL:  function public.is_ok(public.mood) depends on" in str(err.value)
    assert "drop cascades" not in str(err.value)


def test_a_lost_connection_reports_the_end_of_stderr(tmp_path):
    # A killed backend sends no ERROR; the notices before it say nothing.
    notices = "".join(f"NOTICE:  drop cascades to view v{i}\n" for i in range(40))
    stderr = notices + "psql:restore.sql:99: server closed the connection unexpectedly\n"
    run = _PgRun(live=LIVE_MIGRATED)

    def lost(argv, **kw):
        result = run(argv, **kw)
        return subprocess.CompletedProcess(argv, 2, b"", stderr.encode()) if _is_apply(argv) else result

    with pytest.raises(SourceError, match="server closed the connection unexpectedly"):
        PostgresSource(_cfg(), run=lost).restore(_staged_dump(tmp_path))


def test_a_failed_script_generation_never_reaches_the_database(tmp_path):
    run = _PgRun(live=LIVE_MIGRATED, generate_rc=1)
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


def test_keep_acl_applies_to_the_replacing_restore(tmp_path):
    run = _PgRun(live=LIVE_MIGRATED)
    PostgresSource(_cfg(keep_acl=True), run=run).restore(_staged_dump(tmp_path))
    generate = next(c for c in run.calls if c[0] == "pg_restore" and "--file" in c)
    assert "--no-acl" not in generate and "--clean" in generate


def test_keep_acl_accepts_an_interpolated_string():
    assert PostgresSource(_cfg(keep_acl="true")).cfg.keep_acl is True


def test_plain_dumps_skip_the_catalog_query(tmp_path):
    (tmp_path / "logto.sql.gz").write_bytes(gzip.compress(b"SELECT 1;"))
    run = _PgRun(live=LIVE_MIGRATED)
    PostgresSource(_cfg(dump_format="plain"), run=run).restore(tmp_path)
    assert all("--command" not in c for c in run.calls)
    assert len(run.calls) == 1 and run.calls[0][0] == "psql"
