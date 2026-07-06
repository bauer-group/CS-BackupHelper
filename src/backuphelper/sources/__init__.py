"""Source engines — the primary extension point.

Built-ins: postgres, mariadb, mysql, s3_bucket, filesystem, env_snapshot.
Consuming repos register additional sources via the ``backuphelper.sources``
entry-point group (see plugins.registry).
"""
