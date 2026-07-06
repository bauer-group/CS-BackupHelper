# BackupHelper

> BAUER GROUP central backup engine — one GHCR image that replaces the fleet of
> individually-maintained backup sidecars.

BackupHelper snapshots **pluggable sources** (PostgreSQL, MariaDB, MySQL,
S3-compatible buckets *including per-object metadata*, local filesystems, and an
env whitelist), bundles them into deterministic `tar.gz` archives with a
**sha256 manifest**, applies **retention** (count / age / GFS / smart-last),
optionally **encrypts** them (age/gpg) and ships them to **S3-compatible or
local** storage — on a **cron/interval schedule**, with **notifications** and a
full **restore CLI**.

The design principle: **the core knows _how_ to move bytes safely; the consuming
repo knows _what_ the bytes mean.** Application-specific logic (n8n CLI export,
NocoDB REST export, service quiescing) lives in each repo as a registered
**Source plugin** or **lifecycle hook**, never inside this engine.

## Quick start

```bash
docker run --rm \
  -e INSTANCE_NAME=myapp \
  -e BACKUP_JOBS__0__SOURCES__0__TYPE=postgres \
  -e BACKUP_JOBS__0__SOURCES__0__HOST=db \
  -e BACKUP_JOBS__0__SOURCES__0__DATABASE=app \
  -e BACKUP_JOBS__0__SOURCES__0__USER=app \
  -e DB_PASSWORD=secret \
  -e BACKUP_JOBS__0__SOURCES__0__PASSWORD='${DB_PASSWORD}' \
  -v backup-data:/data \
  ghcr.io/bauer-group/backuphelper:latest --now
```

Most deployments pass the whole config as one inline JSON string — see
[`docker-compose.yml`](docker-compose.yml).

## Configuration (three layers, highest precedence wins)

1. **Discrete env vars** — `BACKUP_JOBS__0__RETENTION__COUNT=30` (nested with `__`).
2. **`BACKUP_CONFIG_JSON`** — the entire (multi-job) config inline, no host file.
   Base64 variant: `BACKUP_CONFIG_JSON_BASE64`.
3. **`BACKUP_CONFIG_FILE`** — a mounted `/config/backup.json` or `.yaml`.

Secrets are referenced as `${ENV_VAR}` inside the JSON and resolved from the
environment, so they never live in the config literal.

```json
{ "version": 1, "instance_name": "iam",
  "jobs": [{
    "name": "main",
    "sources": [
      {"type": "postgres", "host": "db", "database": "logto", "password": "${DB_PASSWORD}"},
      {"type": "s3", "endpoint": "https://minio:9000", "bucket": "attachments"},
      {"type": "filesystem", "name": "uploads", "path": "/data/uploads", "exclude": ["cache/*"]}
    ],
    "destinations": [{"type": "local"}, {"type": "s3", "bucket": "offsite", "prefix": "iam/"}],
    "schedule": {"mode": "cron", "cron": "15 3 * * *"},
    "retention": {"count": 14, "age_days": 90, "gfs": {"daily": 7, "weekly": 4, "monthly": 6}},
    "encryption": {"mode": "none"},
    "notifications": {"channels": ["webhook", "teams"], "level": "warnings",
                      "webhook": {"url": "https://...", "secret": "${WEBHOOK_SECRET}"}}
  }]
}
```

**Destinations are only `s3` or `local`.** Policy: S3 when configured (off-site),
otherwise local. `local` is always the working store; a `keep-local` toggle
controls whether the local copy survives after a successful S3 upload.

## Sources

| type | backs up | tool |
| --- | --- | --- |
| `postgres` | PostgreSQL 18 | `pg_dump` custom/plain |
| `mariadb` | MariaDB 11/12 | `mariadb-dump` (multi-DB) |
| `mysql` | MySQL 8/9 | `mysqldump` |
| `s3` | S3 bucket + **per-object metadata/tags/content-type** | boto3 |
| `filesystem` | a named path-group (uploads, content, …) | deterministic tar |
| `env` | a whitelist of env vars | json |

Repos add app-specific sources (n8n, NocoDB, GitHub, …) via the
`backuphelper.sources` entry-point group — no engine changes.

## CLI

```
backuphelper                 # scheduler daemon (default)
backuphelper --now           # run every job once and exit
backuphelper create          # snapshot now
backuphelper list            # list snapshots (local + remote)
backuphelper show <id>       # snapshot detail
backuphelper verify <id>     # re-hash against the manifest
backuphelper restore <id>    # restore (destructive; --force to skip prompt)
backuphelper prune           # apply retention (--dry-run / --keep N)
backuphelper config print --redacted   # show effective config, secrets masked
backuphelper healthcheck     # exit 0 if last backup is fresh
```

## Adopting it in a repo (meta-layer)

Replace the repo's bespoke backup container with a ~20-line meta-Dockerfile:

```dockerfile
FROM ghcr.io/bauer-group/backuphelper:1
ARG PG_CLIENT_VERSION=18
LABEL org.opencontainers.image.title="MyApp Backup"
# Sources/destinations/schedule come from env or BACKUP_CONFIG_JSON in compose.
```

## Development

```bash
python -m venv .venv && ./.venv/Scripts/pip install -e ".[test]"
./.venv/Scripts/pytest -q
```

Tests are a hard build gate: the production image cannot be built unless
`pytest` passes (multi-stage `COPY --from=test`).
