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
| `name` | `"database"` | component name / output basename |

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

**Restore.** Supported. A `custom` dump is replayed with `pg_restore --clean --if-exists --no-owner --no-acl --single-transaction`; a `.sql.gz` is gunzipped and streamed into `psql`. Restore is destructive against the target database.

---

## `mariadb`

Logical dump of one or more MariaDB databases. A single Alpine `mariadb-client` covers MariaDB 11/12 (and MySQL 8/9) via `mariadb-dump`, with a `mysqldump` fallback. The password is passed via the `MYSQL_PWD` environment variable, never on the command line. Dumps are written as `<name>.sql.gz`. Dump flags are fixed: `--single-transaction --quick --routines --triggers --events --no-tablespaces --default-character-set=utf8mb4`.

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

---

## `mysql`

MySQL 8/9 via the same MySQL-family implementation as `mariadb`. Identical fields and mechanics — only the binary preference differs: `mysqldump` is tried first (fallback `mariadb-dump`), and restore prefers `mysql` (fallback `mariadb`). The `kind` field defaults to `"mysql"` here.

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

> **Authentication caveat (MySQL 8.0+).** MySQL 8.0 and later default to the
> `caching_sha2_password` auth plugin, whose **client-side** plugin the Alpine
> `mariadb-client` in the image does **not** ship. Connecting to a default
> MySQL 8/9 fails with `Plugin caching_sha2_password could not be loaded`.
> Choose one:
> - Create the backup user with `mysql_native_password`
>   (`CREATE USER 'backup'@'%' IDENTIFIED WITH mysql_native_password BY '…'`;
>   on MySQL 8.4+ the plugin must first be enabled server-side), **or**
> - Add the Oracle `mysql-client` (or `mydumper`) in your meta-Dockerfile for
>   full `caching_sha2_password` support.
>
> MariaDB is unaffected. This is verified end-to-end by `scripts/e2e.sh`
> (MySQL 8.0 with `mysql_native_password`).

---

## `s3`

Mirrors a full S3 (or S3-compatible) bucket into a `<name>.tar.gz` component — and, unlike a plain key-only mirror, **preserves per-object metadata**. For every object it captures the content-type, user metadata, storage class, ETag and object tags into a deterministic `metadata.json`, and faithfully re-applies them on restore. Works against any S3-compatible endpoint (AWS, MinIO, Ceph/RGW, R2, B2, Wasabi, Garage) via path-style addressing + SigV4.

| field | default | description |
| --- | --- | --- |
| `bucket` | *(required)* | source bucket name |
| `endpoint` | `null` | S3-compatible endpoint URL; `null` targets AWS |
| `region` | `"eu-central-1"` | region |
| `access_key` | `""` | access key id (empty → default credential chain) |
| `secret_key` | `""` | secret access key |
| `prefix` | `""` | only mirror keys under this prefix |
| `force_path_style` | `true` | path-style addressing (needed for MinIO/Ceph); `false` uses virtual-host style |
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

**Restore.** Supported. Each captured object is re-uploaded with `put_object`, re-applying its content-type (`ContentType`), user metadata (`Metadata`) and tags (`Tagging`) from `metadata.json`. Objects are restored into the configured `bucket`.

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

Arcnames are always relative to `path` (even when `subdirs` narrows the roots), so excludes and the restored layout are anchored to `path`. A missing `path` produces an errored component (the job degrades to a partial snapshot rather than failing outright).

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
