"""Tests for the MariaDB / MySQL logical-dump sources."""

import gzip
import subprocess
from pathlib import Path

from backuphelper.sources.mariadb import MariaDBSource, build_argv, resolve_binary
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
    assert run.calls[0][1].get("stdin") is not None  # dump streamed to stdin
