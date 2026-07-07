How to replace a repo's bespoke backup container with BackupHelper. The goal: every consuming repo keeps only a ~20-line meta-Dockerfile and moves its backup configuration into `BACKUP_CONFIG_JSON` (or a mounted file), so backup logic is maintained once, centrally. See [deployment](deployment.md) for the meta-Dockerfile pattern and [plugins](plugins.md) for app-specific sources.

## Why

The fleet's backup sidecars drifted: some sign webhooks with HMAC, others send the secret in plaintext; several have no integrity manifest; only one does retry/backoff. Consolidating onto one image makes every repo a strict superset **and** fixes those inconsistencies in one move — while application-aware logic stays in each repo via a Source plugin or lifecycle hook.

## The pattern

Each repo keeps its Dockerfile, reduced to a meta-layer:

```dockerfile
FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest

# ---------------------------------------------------------------------------
# Metadata Labels
# ---------------------------------------------------------------------------
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.title="CS-IAMStack Database-Backup"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/CS-IAMStack"
LABEL org.opencontainers.image.version="1.0.0"

# Inherit ENTRYPOINT, USER, ENV, HEALTHCHECK from the base image.
# Sources / destinations / schedule come from env or BACKUP_CONFIG_JSON in compose.
```

Track `:latest` so a new central-engine release flows into the consuming image on
its next build. The central image already ships the postgres/mariadb clients,
tini, a non-root user and the healthcheck — the meta-layer adds only labels (and,
if needed, an extra client via `apk add`).

The compose service points at that image and supplies the job config inline — see [docker-compose.yml](../docker-compose.yml) and [docker-compose.sidecar.yml](../docker-compose.sidecar.yml).

## Migration checklist (per repo)

1. Repoint the repo's backup Dockerfile `FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:<ver>` and keep only OCI labels (+ `PG_CLIENT_VERSION` or extra clients if needed).
2. Move the backup config from the old env vars into `BACKUP_CONFIG_JSON` in the compose service (secrets as `$${VAR}`).
3. `docker compose run --rm backup --now` and confirm a snapshot + sidecar manifest appear; `backuphelper verify <id>`.
4. Confirm a restore into a staging target (`restore <id> --force`) before decommissioning the old container.
5. For app-specific export/restore (n8n CLI, NocoDB REST), add a Source plugin in the meta-layer — see [plugins](plugins.md).

## Target repos

Effort to migrate, and what each repo's meta-layer sets (the engine supplies everything else):

| Effort | Repo | Meta-layer sets |
| --- | --- | --- |
| trivial | `SaaS-Projects/CovalidaIAM` | OCI labels only, repoint `FROM` |
| low | `Container-Solution/IAMStack` | source=postgres(logto), HMAC webhook secret, S3 target |
| low | `Container-Solution/IAM` (Zitadel) | source=postgres(zitadel) |
| low | `Production+Development/SonarQube` | source=postgres; normalize plaintext webhook → HMAC |
| low | `Container-Solution/ZAMMAD` | source=[postgres, filesystem:/opt/zammad/storage] |
| low | `Demo-Projects/ContainerBackupPostgreSQL` | source=postgres, dest=[local,s3] |
| medium | `Container-Solution/Outline` | source=[postgres, s3:attachments], dest=[local,s3] |
| medium | `Container-Solution/DocumentSigning` | source=[postgres, s3, env-snapshot] |
| medium | `Container-Solution/NocoDB` | source=[postgres, filesystem]; NocoDB REST exporter as a plugin |
| medium | `Container-Solution/WordPressStack` | source=[mariadb, filesystem:uploads, filesystem:content] |
| high | `Container-Solution/n8n` | +nodejs/npm/n8n; n8n-CLI source plugin |
| high | `Container-Solution/GitHubBackup` | git/LFS/wiki engine stays a GitHub source plugin |
| high | `Internal-Projects/BAUERGROUP.HardwareIDAllocator` | .NET→Python re-platform or NDJSON mode |

> `Internal-Projects/CanvaBackupRunner` is intentionally **out of scope** — it is
> a standalone SaaS-export runner and stays standalone; it is not migrated onto
> BackupHelper.

Start with the trivial/low tier (pure DB backups) to prove the model, then the medium tier (DB + files/objects), and finally the high tier where an app-specific exporter becomes a plugin.

## What gains you get for free

Repos that migrate inherit capabilities their old sidecar lacked: a sha256 integrity manifest + `verify`, normalized HMAC-SHA256 webhook signing, retry/backoff on network calls, optional client-side encryption, count/age/GFS/smart retention, a functional healthcheck, and the full restore CLI.
