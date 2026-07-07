# BackupHelper

> BAUER GROUP central backup engine — one GHCR image that replaces the fleet of
> individually-maintained backup sidecars.

BackupHelper snapshots **pluggable sources** (PostgreSQL, MariaDB, MySQL,
S3-compatible buckets *including per-object metadata*, local filesystems and an
env whitelist), bundles them into deterministic `tar.gz` archives with a
**sha256 manifest**, applies **retention** (count / age / GFS / smart-last),
optionally **encrypts** them (age/gpg) and ships them to **S3-compatible or
local** storage — on a **cron/interval schedule**, with **notifications** and a
full **restore CLI**.

**Design principle:** the core knows *how* to move bytes safely; the consuming
repo knows *what* the bytes mean. Application-specific logic (n8n CLI export,
NocoDB REST export, service quiescing) lives in each repo as a registered
[Source plugin](docs/plugins.md) or lifecycle hook — never inside this engine.

## Features

- **Sources**: PostgreSQL 18, MariaDB 11/12, MySQL 8/9, S3 buckets (with
  per-object metadata/tags/content-type), filesystem path-groups, env whitelist
  — combinable into one atomic snapshot.
- **Destinations**: local + any S3-compatible target (MinIO, R2, B2, Wasabi,
  Ceph, Garage) via a hand-rolled equal-chunk multipart uploader.
- **Integrity**: deterministic archives + sha256 manifest (embedded + sidecar)
  + a `verify` command.
- **Retention**: count, age, GFS (grandfather-father-son) and smart-last,
  applied independently per destination.
- **Notifications**: email, HMAC-signed webhook, Teams, Slack, Discord,
  ntfy/Gotify and a healthchecks.io dead-man's-switch — severity-gated with
  per-channel fault isolation.
- **Encryption**: optional client-side age/gpg before off-site upload.
- **Restore**: full restore CLI for every source type.
- **Ops**: non-root, tini, a functional healthcheck, structured logging with
  secret redaction, and a test-gated multi-stage image.

## Quick start

```bash
cp .env.example .env      # set DB_PASSWORD and (optionally) S3 credentials
docker compose --profile backup up -d
docker compose run --rm backup --now      # take a snapshot now
docker compose run --rm backup list       # list snapshots
docker compose run --rm backup verify <id>
```

Most deployments pass the whole job inline as `BACKUP_CONFIG_JSON` — see
[docker-compose.yml](docker-compose.yml) and [docker-compose.sidecar.yml](docker-compose.sidecar.yml).

## Configuration in 30 seconds

Config comes from (highest precedence first): discrete `BACKUP_..__` env
overrides → inline `BACKUP_CONFIG_JSON` → mounted `BACKUP_CONFIG_FILE` → model
defaults. Secrets are referenced as `${VAR}` and resolved from the environment,
never written into the config text.

```json
{
  "instance_name": "app",
  "jobs": [{
    "name": "main",
    "sources": [
      {"type": "postgres", "host": "db", "database": "app", "password": "${DB_PASSWORD}"},
      {"type": "filesystem", "name": "uploads", "path": "/uploads"}
    ],
    "destinations": [{"type": "local"}, {"type": "s3", "bucket": "offsite", "prefix": "app/"}],
    "schedule": {"mode": "cron", "cron": "15 3 * * *"},
    "retention": {"count": 14, "age_days": 90}
  }]
}
```

Ready-to-adapt configs for common cases live in [examples/config/](examples/config/).

## Documentation

| Guide | What it covers |
| --- | --- |
| [configuration.md](docs/configuration.md) | Config layers, secrets, the full schema |
| [sources.md](docs/sources.md) | Every source type and its options |
| [destinations.md](docs/destinations.md) | Local + S3, the multipart uploader |
| [retention.md](docs/retention.md) | count / age / GFS / smart-last |
| [notifications.md](docs/notifications.md) | Channels + webhook HMAC signing |
| [encryption.md](docs/encryption.md) | age/gpg client-side encryption |
| [cli.md](docs/cli.md) | Every command and exit code |
| [restore.md](docs/restore.md) | Disaster-recovery walkthrough |
| [deployment.md](docs/deployment.md) | Meta-Dockerfile pattern, healthcheck, security |
| [plugins.md](docs/plugins.md) | Source-plugin + lifecycle-hook extension API |
| [migration.md](docs/migration.md) | Adopting BackupHelper across the fleet |

## Adopting it in a repo

Replace a repo's bespoke backup container with a ~20-line meta-Dockerfile:

```dockerfile
FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest
ARG PG_CLIENT_VERSION=18
LABEL org.opencontainers.image.title="MyApp Backup"
# Sources/destinations/schedule come from env or BACKUP_CONFIG_JSON in compose.
```

See [migration.md](docs/migration.md) for the full fleet migration plan.

## Development

```bash
python -m venv .venv && ./.venv/Scripts/pip install -e ".[test]"
./.venv/Scripts/pytest -q
```

Tests are a hard build gate: the production image cannot be built unless
`pytest` passes (multi-stage `COPY --from=test`).
