Sources are the *what to capture* side of a job. Each source knows how to dump one backend into a staging directory and hand back the artifacts it produced; the engine then hashes, bundles, (optionally) encrypts and ships them. See [configuration](configuration.md) for how sources fit into a job and [destinations](destinations.md) for where the bundle lands.

## Overview

A job's `sources` is a list. Every entry is an object with a `type` discriminator plus that source's own config keys — the spec is *open* (`extra="allow"`), so plugin source types validate their own fields without engine changes.

| type | backs up | tool | restore |
| --- | --- | --- | --- |
| `postgres` | one PostgreSQL database | `pg_dump` (custom or plain) | yes |
| `mariadb` | one or more MariaDB databases | `mariadb-dump` (fallback `mysqldump`) | yes |
| `mysql` | one or more MySQL databases | `mysqldump` (fallback `mariadb-dump`) | yes |
| `s3` | a full S3 bucket **with per-object metadata** | boto3 | yes |
| `filesystem` | one named path-group → deterministic `tar.gz` | tar/gzip | yes |
| `env` | a whitelist of environment variables → `env.json` | json | informational only |

**One job, many sources, one snapshot.** A job may list any number of sources of any mix of types. They all stage into the *same* directory and are captured together into a single atomic bundle (`<snapshot-id>.tar.gz`) with one shared `sha256` manifest — so a database dump, its uploads and its env whitelist restore as one consistent point in time.

Every source's output filename is derived from its component `name` (or, for databases, the database name). Restore matches a bundle component back to its source by that name, so keep `name` stable across runs.

**A failing source never disappears.** Whether a source reports an error (a failed `pg_dump`, a missing `path`) or raises one (a `PermissionError`, an unreachable plugin backend), the manifest still lists its component with size `0`, an empty `sha256` and the error text, and `show <id>` makes the gap visible. The other sources' components are still stored and restorable, but the run ends in `error` — exit `1`, an error alert and an unhealthy container until a complete snapshot exists (see [run status](cli.md#run-status); up to 1.7.6 this was a `warning`). Partial output of a raising source is discarded, not bundled. To leave a source out on purpose, disable it with `"enabled": false`: a disabled source produces no component and does not affect the status.

---

## `postgres`

Dumps a single PostgreSQL database with `pg_dump`. The password is placed in the subprocess environment as `PGPASSWORD` (along with `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGSSLMODE`) — **never on the command line**, so it never appears in `ps` output. The `custom` format writes a compressed `.dump`; the `plain` format writes SQL that the engine gzips to `.sql.gz`.

| field | default | description |
| --- | --- | --- |
| `host` | `"database-server"` | DB host → `PGHOST` |
| `port` | `5432` | DB port (1–65535) → `PGPORT` |
| `database` | `"postgres"` | database name → `PGDATABASE`; `db` is accepted as an alias |
| `user` | `"postgres"` | role → `PGUSER` |
| `password` | `""` | password → `PGPASSWORD` env, not argv |
| `ssl_mode` | `"disable"` | → `PGSSLMODE` |
| `dump_format` | `"custom"` | `custom` (`pg_dump --format=custom --compress=6`) or `plain` (SQL, gzipped) |
| `timeout` | `1800` | dump timeout in seconds (1–14400) |
| `name` | `null` | component name / output basename (`<name>.dump` or `<name>.sql.gz`); defaults to the `database` name |
| `exclude_table_data` | `[]` | tables whose rows are left out while their structure is kept (`pg_dump --exclude-table-data`); a list or a comma-separated string |
| `keep_acl` | `false` | dump the privileges (`GRANT`/`REVOKE`, `ALTER DEFAULT PRIVILEGES`) and re-apply them on restore — see [Privileges](#privileges-keep_acl) |

```json
{
  "sources": [
    {
      "type": "postgres",
      "host": "db",
      "database": "app",
      "user": "app",
      "password": "${DB_PASSWORD}",
      "dump_format": "custom"
    }
  ]
}
```

**Restore.** Supported. A `custom` dump is replayed with `pg_restore --clean --if-exists --no-owner --no-acl --single-transaction` (without `--no-acl` when `keep_acl` is set); a `.sql.gz` is gunzipped and streamed into `psql`. Restore is destructive against the target database.

**Partitioned tables.** `pg_restore --clean` cannot restore over partitioned tables that exist in the target database: its clean phase drops each partition's primary key on its own, PostgreSQL refuses (`cannot drop inherited constraint`), and the single transaction rolls back — nothing is damaged, nothing is restored. Before a `custom` restore the source therefore asks the target for its partitioned tables. When the dump recreates some of them, the restore runs in **one** `psql --single-transaction` instead: first `DROP TABLE … CASCADE` for exactly those tables (their partitions go with them), then the dump's own clean-and-create script, generated by `pg_restore --clean --if-exists` into a file next to the dump. Any error rolls everything back, the drops included. `CASCADE` also drops objects that depend on such a table — foreign keys, views; the dump recreates all of its own, and psql's `drop cascades to …` notices are logged so an object that only existed in the target is visible. Without partitioned tables in the target (a fresh database, or no partitioning at all) the restore is the plain `pg_restore` above. Plugins that worked around this (CS-IAM's `zitadel-postgres`) can switch back to `postgres`.

#### Privileges (`keep_acl`)

By default the dump and the restore both run with `--no-acl`: `--clean` drops and recreates every table, sequence, view and function, and the recreated objects carry only their owner's privileges. An application that connects as a **restricted runtime role** — a role the owner granted `SELECT`/`INSERT`/… on its tables — therefore gets `permission denied` after a restore until the grants are applied again.

With `"keep_acl": true` the dump keeps the privileges (`GRANT`/`REVOKE` on every object, `ALTER DEFAULT PRIVILEGES`) and the restore re-applies them. What to know before switching it on:

- It takes effect for **snapshots taken after** the switch: an older snapshot holds no privileges, so restoring it still leaves the grants out. `show <id>` lists `"acl": true` in the metadata of components that carry them.
- **Roles are not part of a database dump** — they belong to the cluster. Every role a privilege names must exist in the target, otherwise the restore fails (`role "…" does not exist`) and rolls back. In place that is a given; on a new host create the roles first (e.g. in a `pre_restore` hook or the database's init scripts).
- The privileges are applied by the restoring `user`, who must be allowed to grant them: the owner of the objects (the restore recreates them, so it owns them) or a superuser.
- Ownership is still not restored (`--no-owner`): the restoring `user` owns every recreated object, as before.

```json
{"type": "postgres", "host": "db", "database": "app", "user": "app",
 "password": "${DB_PASSWORD}", "keep_acl": true}
```

---

## `mariadb`

Logical dump of one or more MariaDB databases. A single Alpine `mariadb-client` covers MariaDB and MySQL via `mariadb-dump`, with a `mysqldump` fallback — CI round-trips MariaDB 11.4, 11.8 and 13 and MySQL 8.0, 8.4 and 26.7 through it (`scripts/e2e.sh`). The password is passed via the `MYSQL_PWD` environment variable, never on the command line. Dumps are written as `<name>.sql.gz`. Dump flags are fixed: `--single-transaction --quick --routines --triggers --events --no-tablespaces --default-character-set=utf8mb4`.

| field | default | description |
| --- | --- | --- |
| `kind` | `"mariadb"` | family discriminator; set automatically from the source `type` |
| `host` | `"database"` | DB host |
| `port` | `3306` | DB port (1–65535) |
| `database` | `null` | single database name (omit `--databases`) |
| `databases` | `[]` | list of databases → `--databases db1 db2 …` (multi-DB dump) |
| `user` | `"root"` | user |
| `password` | `""` | password → `MYSQL_PWD` env, not argv |
| `binary` | `null` | explicit dump binary override (skips auto-detection) |
| `name` | `null` | component name; defaults to the `database` name, else `"database"` |
| `timeout` | `2700` | dump timeout in seconds (1–14400) |

```json
{
  "sources": [
    {
      "type": "mariadb",
      "host": "mariadb",
      "databases": ["wordpress", "zammad"],
      "user": "root",
      "password": "${MYSQL_ROOT_PASSWORD}",
      "name": "sites"
    }
  ]
}
```

**Multi-DB.** Set `databases` to dump several schemas into one component; leave it empty and set `database` to dump exactly one. If both are empty the dump targets the server defaults.

**Restore.** Supported. Restore uses the interactive client (`mariadb`, fallback `mysql`) and streams the gunzipped `.sql.gz` into it via stdin. If `database` is set it is passed as the target schema.

The dump keeps the `DEFINER` of every trigger, view, event and routine. The restoring `user` must be allowed to create objects for those definers — be the definer itself (objects the application user created), or hold `SET USER` (MariaDB) / `SET_ANY_DEFINER` (MySQL 8.2+; `SET_USER_ID` before) — otherwise the restore stops at the first such object with `Access denied; you need … SET USER`.

---

## `mysql`

MySQL (8.0 and later, including the 26.x calendar versions) via the same MySQL-family implementation as `mariadb`. Identical fields and mechanics — only the binary preference differs: `mysqldump` is tried first (fallback `mariadb-dump`), and restore prefers `mysql` (fallback `mariadb`). The `kind` field defaults to `"mysql"` here.

```json
{
  "sources": [
    {
      "type": "mysql",
      "host": "mysql",
      "database": "shop",
      "user": "root",
      "password": "${MYSQL_ROOT_PASSWORD}"
    }
  ]
}
```

**Restore.** Supported, as for `mariadb`.

**Backup user.** The dump needs `SELECT`, `SHOW VIEW`, `TRIGGER` and `EVENT` on the dumped databases — not `LOCK TABLES` (every pass runs with `--single-transaction`) and not `PROCESS` (`--no-tablespaces`). To dump stored functions and procedures it must also be able to read their bodies: be their definer, or hold `SHOW_ROUTINE` (MySQL 8.0.20+) or the global `SELECT` privilege. To a user who holds only `EXECUTE`, `ALTER ROUTINE` or `CREATE ROUTINE` on a routine, MySQL shows the routine without its body; the dump then fails with `… has insufficient privileges to SHOW CREATE PROCEDURE …` rather than leave the routine out. CI backs up with such a minimal user (`SELECT, SHOW VIEW, TRIGGER, EVENT` on the database plus `SHOW_ROUTINE`) and checks that a user without `SHOW_ROUTINE` fails.

**MySQL 26 and later.** `mariadb-dump` takes every server version from 10.3 up for MariaDB and, with `--routines`, asks the server for MariaDB packages (`SHOW PACKAGE STATUS`). MySQL's calendar versions (26.x) are above that, so the query is a syntax error and the whole dump failed (`Couldn't execute 'SHOW PACKAGE STATUS …' (1064)`). Before each dump the source therefore reads `SELECT VERSION()`; on a MySQL server from version 10 up (MariaDB reports `…-MariaDB`) it dumps the tables, data, triggers and events with `--skip-routines`, then the functions and procedures in a second pass (`--single-transaction --routines --no-create-info --no-data --force`) in which that package query is the only error accepted. Any other report fails the component, as in the single pass — including a routine whose body the backup user may not read (`… has insufficient privileges to SHOW CREATE PROCEDURE …`), which `--force` would otherwise leave out of the dump. Both passes go into the same `<name>.sql.gz` and restore as one; the component's metadata shows `"routines": "separate pass (MySQL 26+)"`. Like the first pass, the second runs in a transaction of its own (a moment after the data) and takes no table locks, so it neither blocks writers nor needs the `LOCK TABLES` privilege. On every other server the dump is the single pass described above.

> **Authentication.** MySQL logs users in with `caching_sha2_password` by
> default (since 8.0; `mysql_native_password` is disabled in 8.4 and removed in
> 9.0). Alpine's `mariadb-client` comes without any client authentication
> plugin, so the image also installs `mariadb-connector-c`, which provides
> `caching_sha2_password`, `sha256_password`, `client_ed25519`, `parsec` and
> `dialog`. A backup user with the server's default authentication works
> without further setup. Up to 1.7.7 the image lacked the plugins: a login
> failed with `Plugin caching_sha2_password could not be loaded`, and only a
> `mysql_native_password` user worked — such a user keeps working.

---

## `s3`

Mirrors a full S3 (or S3-compatible) bucket into a `<name>.tar.gz` component — and, unlike a plain key-only mirror, **preserves per-object metadata**. For every object it captures the content headers (`Content-Type`, `Content-Disposition`, `Cache-Control`, `Content-Encoding`, `Content-Language`), user metadata, storage class, ETag and object tags into a deterministic `metadata.json`, and faithfully re-applies them on restore. Works against any S3-compatible endpoint (AWS, MinIO, Ceph/RGW, R2, B2, Wasabi, Garage) via path-style addressing + SigV4.

| field | default | description |
| --- | --- | --- |
| `bucket` | *(required)* | source bucket name |
| `endpoint` | `null` | S3-compatible endpoint URL; `null` targets AWS |
| `region` | `"eu-central-1"` | region |
| `access_key` | `""` | access key id (empty → default credential chain) |
| `secret_key` | `""` | secret access key |
| `prefix` | `""` | only mirror keys under this prefix |
| `force_path_style` | `true` | path-style addressing (needed for MinIO/Ceph); `false` uses virtual-host style |
| `verify_tls` | `true` | verify the endpoint's TLS certificate; `false` switches verification off (insecure, logs a warning) |
| `ca_bundle` | `null` | path to a PEM file with the CA certificate(s) to trust for the endpoint, e.g. a private CA |
| `name` | `"s3"` | component name |

```json
{
  "sources": [
    {
      "type": "s3",
      "endpoint": "https://minio:9000",
      "bucket": "attachments",
      "access_key": "${S3_ACCESS_KEY}",
      "secret_key": "${S3_SECRET_KEY}",
      "prefix": "uploads/"
    }
  ]
}
```

`verify_tls` and `ca_bundle` work as for the s3 destination, including the security warning about switching verification off: see [TLS certificate verification](destinations.md#tls-certificate-verification).

**Restore.** Supported. Each captured object is re-uploaded with `put_object`, re-applying its content headers (`ContentType`, `ContentDisposition`, `CacheControl`, `ContentEncoding`, `ContentLanguage`), user metadata (`Metadata`) and tags (`Tagging`) from `metadata.json`. Objects are restored into the configured `bucket`.

The content headers decide how a client treats an object: Outline, for example, stores attachments of types a browser would render (HTML, SVG) with `Content-Disposition: attachment`, so they are downloaded instead of rendered. Snapshots taken with 1.7.7 or earlier captured only `Content-Type`; their objects restore without the other four headers.

**Tags that cannot be read.** Reading an object's tags needs the `s3:GetObjectTagging` permission, and some S3-compatible stores have no object tagging at all. When `get_object_tagging` is refused (`AccessDenied`, `NotImplemented`, `MethodNotAllowed`, `NotSupported`, … or a bare HTTP 403/405/501), the object is still backed up: its `tags` are recorded as `null` in `metadata.json` (an object without tags has `{}`), and the component carries one warning for all of them — `tags of 3 of 120 objects (get_object_tagging: AccessDenied); these objects restore without tags` — so the job ends in `warning` with an alert. Those objects restore without tags. Any other error while reading tags (a server error, a timeout) still fails the component.

---

## `filesystem`

Archives **one named path-group** into a byte-deterministic `<name>.tar.gz` (sorted members, `mtime=0`, zeroed uid/gid/owner, no gzip filename), so identical trees hash identically across runs. List several `filesystem` sources in one job for several independent path-groups (e.g. WordPress uploads, WordPress content, ZAMMAD storage).

| field | default | description |
| --- | --- | --- |
| `name` | `"files"` | component name / archive basename |
| `path` | *(required)* | root directory to archive |
| `subdirs` | `null` | if set, archive only these subdirectories of `path` |
| `exclude` | `[]` | `fnmatch` globs matched against each member's relative posix path |

```json
{
  "sources": [
    {
      "type": "filesystem",
      "name": "uploads",
      "path": "/data/wordpress",
      "subdirs": ["wp-content/uploads", "wp-content/plugins"],
      "exclude": ["*/cache/*", "*.tmp"]
    }
  ]
}
```

Arcnames are always relative to `path` (even when `subdirs` narrows the roots), so excludes and the restored layout are anchored to `path`. A missing `path` produces an errored component: the run ends in `error`, while the other sources' components are still stored. A file or directory below `path` that the backup user cannot read is skipped, never silently: everything readable is still archived and restorable, the skipped entries are listed in the component's `metadata.warnings` (shown by `show <id>`, at most 20 plus a count), and the job degrades to `warning` with an alert (`<name>: incomplete, skipped: …`). Only an unreadable `path` itself fails the whole component (`PermissionError` in the manifest); an unreadable `subdirs` entry is skipped and reported like any other directory. An unreadable `lost+found` (the `mkfs` artefact on a mounted filesystem root) is skipped without a warning. To leave a directory out on purpose, exclude it with a `<dir>/*` pattern (e.g. `"lost+found/*"`): a directory matched by such a pattern is not entered at all and causes no warning.

**Restore.** Supported. The extracted tree is overlaid file-by-file onto `path` (parent directories created as needed). This is an overlay copy — it does not delete files that are absent from the archive.

---

## `env`

Captures a whitelist of environment variables into a deterministic `env.json` (sorted keys). Only explicitly whitelisted variables are captured — either exact names or `fnmatch` globs (case-sensitive) — so secrets outside the whitelist never enter the snapshot.

| field | default | description |
| --- | --- | --- |
| `name` | `"env"` | component name / output basename |
| `whitelist` | `[]` | exact variable names or case-sensitive `fnmatch` globs to capture |

```json
{
  "sources": [
    {
      "type": "env",
      "name": "app-env",
      "whitelist": ["APP_*", "DATABASE_URL", "S3_ENDPOINT"]
    }
  ]
}
```

**Restore.** Informational only. `env` components are captured and bundled, but the engine does **not** auto-apply them on restore — reinstating environment variables is an app concern, left to a repo lifecycle hook (e.g. an `ENCRYPTION_KEY` cross-check) rather than this source.

---

## Extending

Repos add app-specific sources (n8n, NocoDB, GitHub, …) via the `backuphelper.sources` entry-point group. Because `sources` entries are open specs, a plugin source validates and preserves its own config keys with no changes to the engine. See [configuration](configuration.md) for the full job model.
