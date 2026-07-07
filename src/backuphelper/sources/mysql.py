"""MySQL source — MySQL 8/9 via the shared MySQL-family dump implementation.

Uses ``mysqldump`` first (``mariadb-dump`` fallback). Identical logical-dump
mechanics to MariaDB; only the binary preference differs.
"""

from __future__ import annotations

from .mariadb import MariaDBSource


class MySQLSource(MariaDBSource):
    type = "mysql"
