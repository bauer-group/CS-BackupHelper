"""Tests for the MariaDB / MySQL logical-dump sources."""

import gzip
import subprocess
from pathlib import Path

import pytest

from backuphelper.sources.mariadb import (
    MariaDBSource,
    build_argv,
    build_routines_argv,
    needs_separate_routines,
    resolve_binary,
)
from backuphelper.sources.mysql import MySQLSource


def _cfg(**over):
    base = {"type": "mariadb", "host": "db", "port": 3306, "database": "wordpress",
            "user": "wp", "password": "pw"}
    base.update(over)
    return base


def test_password_goes_to_env_not_argv():
    src = MariaDBSource(_cfg())
    argv = build_argv(src.cfg, binary="mariadb-dump")
    assert "pw" not in " ".join(argv)
    assert src.build_env()["MYSQL_PWD"] == "pw"


def test_argv_has_consistency_and_completeness_flags():
    src = MariaDBSource(_cfg())
    argv = build_argv(src.cfg, binary="mariadb-dump")
    for flag in ("--single-transaction", "--quick", "--routines", "--triggers",
                 "--events", "--no-tablespaces", "--default-character-set=utf8mb4"):
        assert flag in argv
    assert "wordpress" in argv


def test_multi_database_fanout_uses_databases_flag():
    src = MariaDBSource(_cfg(database=None, databases=["a", "b"]))
    argv = build_argv(src.cfg, binary="mariadb-dump")
    assert "--databases" in argv and "a" in argv and "b" in argv


def test_mariadb_prefers_mariadb_dump_binary():
    calls = {"mariadb-dump": "/usr/bin/mariadb-dump", "mysqldump": "/usr/bin/mysqldump"}
    assert resolve_binary(MariaDBSource(_cfg()).cfg, which=calls.get) == "/usr/bin/mariadb-dump"


def test_mysql_prefers_mysqldump_binary():
    calls = {"mariadb-dump": "/usr/bin/mariadb-dump", "mysqldump": "/usr/bin/mysqldump"}
    src = MySQLSource({"type": "mysql", "host": "db", "database": "app", "user": "u", "password": "p"})
    assert resolve_binary(src.cfg, which=calls.get) == "/usr/bin/mysqldump"


class _FakeRun:
    def __init__(self, rc=0, stdout=b"-- dump\n", stderr=b""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr

    def __call__(self, argv, **kw):
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def test_produce_writes_gzip_component(tmp_path):
    src = MariaDBSource(_cfg(), run=_FakeRun(), which=lambda _b: "/usr/bin/mariadb-dump")
    c = src.produce(tmp_path)[0]
    assert c.kind == "mariadb" and c.error is None
    assert c.path.name == "wordpress.sql.gz"
    assert gzip.decompress(c.path.read_bytes()) == b"-- dump\n"


def test_produce_failure_returns_errored_component(tmp_path):
    src = MariaDBSource(_cfg(), run=_FakeRun(rc=2, stderr=b"access denied"),
                        which=lambda _b: "/usr/bin/mariadb-dump")
    c = src.produce(tmp_path)[0]
    assert c.error is not None and "access denied" in c.error
    assert c.path is None


class _RecordRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_restore_runs_client_with_gunzipped_dump(tmp_path):
    import gzip
    (tmp_path / "wordpress.sql.gz").write_bytes(gzip.compress(b"SELECT 1;"))
    run = _RecordRun()
    MariaDBSource(_cfg(), run=run, which=lambda _b: "/usr/bin/mariadb").restore(tmp_path)
    argv = run.calls[0][0]
    assert Path(argv[0]).name in ("mariadb", "mysql")
    assert "wordpress" in argv
    stdin = run.calls[0][1].get("stdin")
    assert stdin is not None  # dump streamed to stdin
    # Must feed a real OS file (decompressed), NOT a GzipFile — a subprocess
    # reads the child's stdin fd directly and would get the compressed bytes.
    assert not isinstance(stdin, gzip.GzipFile)


# --- MySQL 26+: routines in a second pass -----------------------------------
# mariadb-dump takes every server >= 10.3 for MariaDB and runs SHOW PACKAGE
# STATUS while dumping routines; on MySQL 26.7 that is a syntax error (1064)
# that failed the whole dump (seen in the e2e run against mysql:26.7).

PACKAGE_ERROR = (b"mysqldump: Couldn't execute 'SHOW PACKAGE STATUS WHERE Db = 'app'': "
                 b"You have an error in your SQL syntax; check the manual that corresponds "
                 b"to your MySQL server version (1064)\n")


@pytest.mark.parametrize("version,expected", [
    ("26.7.0", True),
    ("27.1.0-commercial", True),
    ("8.0.46", False),
    ("8.4.11", False),
    ("9.7.2", False),
    ("11.8.9-MariaDB-ubu2404", False),
    ("13.0.2-MariaDB-ubu2604", False),
    ("", False),
])
def test_needs_separate_routines(version, expected):
    assert needs_separate_routines(version) is expected


class _ServerRun:
    """Answers the version probe, the main dump and the routines-only pass."""

    def __init__(self, version=b"26.7.0\n", version_rc=0, main_rc=0,
                 routines_rc=2, routines_stderr=PACKAGE_ERROR):
        self.version, self.version_rc, self.main_rc = version, version_rc, main_rc
        self.routines_rc, self.routines_stderr = routines_rc, routines_stderr
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if "--execute" in argv:
            return subprocess.CompletedProcess(argv, self.version_rc, self.version, b"")
        if "--no-data" in argv:
            return subprocess.CompletedProcess(argv, self.routines_rc, b"-- routines\n",
                                               self.routines_stderr)
        return subprocess.CompletedProcess(argv, self.main_rc, b"-- tables\n",
                                           b"" if self.main_rc == 0 else b"access denied")


def _mysql(run):
    return MySQLSource({"type": "mysql", "host": "db", "database": "app", "user": "bk",
                        "password": "pw"}, run=run, which=lambda b: f"/usr/bin/{b}")


def test_mysql_26_dumps_the_routines_in_a_second_pass(tmp_path):
    run = _ServerRun()
    c = _mysql(run).produce(tmp_path)[0]

    assert c.error is None
    probe, main, routines = run.calls
    assert probe[0] == "/usr/bin/mysql" and probe[-1] == "SELECT VERSION()"
    assert "--skip-routines" in main and "--routines" not in main
    assert {"--triggers", "--events", "--single-transaction"} <= set(main)
    assert {"--routines", "--no-data", "--no-create-info", "--force"} <= set(routines)
    assert routines[-1] == "app"
    assert gzip.decompress(c.path.read_bytes()) == b"-- tables\n-- routines\n"
    assert c.metadata["routines"] == "separate pass (MySQL 26+)"
    assert "pw" not in " ".join(" ".join(a) for a in run.calls)


def test_a_clean_routines_pass_is_accepted_too(tmp_path):
    # A mariadb-dump that learns MySQL's versions exits 0 here.
    c = _mysql(_ServerRun(routines_rc=0, routines_stderr=b"")).produce(tmp_path)[0]
    assert c.error is None


def test_any_other_routines_error_fails_the_dump(tmp_path):
    stderr = PACKAGE_ERROR + b"mysqldump: Couldn't execute 'SHOW CREATE PROCEDURE `p`': denied (1227)\n"
    c = _mysql(_ServerRun(routines_stderr=stderr)).produce(tmp_path)[0]
    assert c.path is None and "1227" in c.error
    assert list(tmp_path.iterdir()) == []


def test_a_routines_failure_without_the_package_error_fails_the_dump(tmp_path):
    c = _mysql(_ServerRun(routines_rc=2, routines_stderr=b"mysqldump: Got error: 2013: lost connection\n")
               ).produce(tmp_path)[0]
    assert c.path is None and "lost connection" in c.error


# What the client prints besides errors: the TLS notice when the password comes
# from MYSQL_PWD (include/sslopt-vars.h) and the old-name notice when it runs as
# mysqldump (mysys/my_init.c).
TLS_NOTICE = (b"WARNING: option --ssl-verify-server-cert is disabled, because of an "
              b"insecure passwordless login.\n")
NAME_NOTICE = (b"mysqldump: Deprecated program name. It will be removed in a future "
               b"release, use '/usr/bin/mariadb-dump' instead\n")


def test_the_client_notices_do_not_fail_the_routines_pass(tmp_path):
    c = _mysql(_ServerRun(routines_stderr=TLS_NOTICE + NAME_NOTICE + PACKAGE_ERROR)
               ).produce(tmp_path)[0]
    assert c.error is None


# With --force, mariadb-dump reports a problem and goes on, and not every report
# says "error": a routine whose body the user may not read (MySQL shows it only
# to its definer and to holders of SHOW_ROUTINE or global SELECT) is reported as
# "insufficient privileges" and left out of the dump.
@pytest.mark.parametrize("report", [
    b"mysqldump: bk@% has insufficient privileges to SHOW CREATE PROCEDURE `demo_count`!\n",
    b"mysqldump: Got error: 1044: \"Access denied for user 'bk'@'%' to database 'app'\" "
    b"when using LOCK TABLES\n",
    b"mysqldump: Failed to start transaction on connection ID 12\n",
])
def test_any_other_report_in_the_routines_pass_fails_the_dump(tmp_path, report):
    c = _mysql(_ServerRun(routines_stderr=TLS_NOTICE + report + PACKAGE_ERROR)
               ).produce(tmp_path)[0]
    assert c.path is None
    assert report.decode().split(": ", 1)[1].strip() in c.error
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version,version_rc", [
    (b"8.4.11\n", 0), (b"11.8.9-MariaDB-ubu2404\n", 0), (b"", 1),
])
def test_other_servers_keep_the_single_pass(tmp_path, version, version_rc):
    run = _ServerRun(version=version, version_rc=version_rc)
    c = _mysql(run).produce(tmp_path)[0]
    assert c.error is None and "routines" not in c.metadata
    dumps = [a for a in run.calls if "--execute" not in a]
    assert len(dumps) == 1 and "--routines" in dumps[0]
    assert gzip.decompress(c.path.read_bytes()) == b"-- tables\n"


def test_routines_argv_targets_every_database():
    src = MariaDBSource(_cfg(database=None, databases=["a", "b"]))
    argv = build_routines_argv(src.cfg, "mariadb-dump")
    assert argv[-3:] == ["--databases", "a", "b"] and "--no-create-db" in argv
