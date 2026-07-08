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

1. Repoint the repo's backup Dockerfile `FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest` and keep only OCI labels (+ `PG_CLIENT_VERSION` or extra clients if needed). Track `:latest` so a new engine release flows into the consuming image on its next build.
2. Move the backup config from the old env vars into `BACKUP_CONFIG_JSON` in the compose service (secrets as `$${VAR}`).
3. **Reconcile the repo's CI config.** Deleting the old sidecar's `src/<backup>/` sources leaves three config files pointing at paths that no longer exist — this is the step most easily forgotten, and it surfaces as red CI *after* the merge, not during it. Commit these as `ci(backup): …` (not `fix:`, so semantic-release does not mint a release for a config-only reconciliation):
   - **`.github/config/docker-base-image-monitor/base-images.json`** — the backup image now consumes `backuphelper:latest`, not `python:*-alpine`. Add a `backuphelper` entry (image `ghcr.io/bauer-group/cs-backuphelper/backuphelper`, tag `latest`, variable `BACKUPHELPER_LATEST_DIGEST`). **Keep** the existing python entry *only if* another component in the repo still builds `FROM python` (e.g. IAM's `directory-sync`) — then add alongside it; **otherwise replace** it. A stale-but-green monitor is the trap: it tracks an image nothing builds on and never fires on a real engine release.
   - **`.github/dependabot.yml`** — remove the `pip` ecosystem entry for the backup dir. The migration deletes `requirements*.txt`, so the pip watcher has no manifest and Dependabot errors with "no manifest found". Leave the `docker` entry (it now inertly tracks the `:latest` base) but correct any stale `python:*-alpine` comment.
   - **`sonar-project.properties`** (only repos that run Sonar) — `sonar.sources` / `sonar.tests` pointing at the deleted `src/<backup>/{src,tests}` fail Code Quality with `sonar-scanner` exit 3 ("folder does not exist"). Point `sonar.sources` at what survives (e.g. `scripts`) and drop `sonar.tests` if no test folder remains.
4. `docker compose run --rm backup --now` and confirm a snapshot + sidecar manifest appear; `backuphelper verify <id>`.
5. Confirm a restore into a staging target (`restore <id> --force`) before decommissioning the old container.
6. For app-specific export/restore (n8n CLI, NocoDB REST), add a Source plugin in the meta-layer — see [plugins](plugins.md).

> **Transient CI noise vs. real breakage.** Two failures during a migration are *not* repo defects and self-heal on re-run: a first-pull `403 Forbidden` for `backuphelper:latest` right after a fresh publish (the package is **public** — this is GHCR permission propagation), and a `503` from the Sonar scanner (the self-hosted SonarQube server momentarily down). Only a deterministic error tied to a deleted path — e.g. `sonar-scanner` exit 3 on a missing `sonar.tests` folder — is yours to fix. Re-run before reaching for a code change.

## Target repos

Effort to migrate, and what each repo's meta-layer sets (the engine supplies everything else):

| Effort | Repo | Status — meta-layer sets |
| --- | --- | --- |
| trivial | `SaaS-Projects/CovalidaIAM` | ✅ done — consumer, mirrors `cs-iamstack/database-backup` |
| low | `Container-Solution/IAMStack` | ✅ done — canonical meta-layer + consumer template |
| low | `Container-Solution/IAM` (Zitadel) | ✅ done — source=postgres(zitadel) |
| low | `Production+Development/SonarQube` | ✅ done — source=postgres; webhook plaintext → HMAC |
| low | `Container-Solution/ZAMMAD` | ✅ done — source=[postgres, filesystem:/opt/zammad/storage] |
| low | `Demo-Projects/ContainerBackupPostgreSQL` | ⏸ deferred — needs engine S3 Object-Lock/WORM (it is the donor) |
| medium | `Container-Solution/Outline` | ✅ done — source=[postgres, s3:attachments], dest=[local,s3] |
| medium | `Container-Solution/DocumentSigning` | ✅ done — source=[postgres(custom), s3:documents, env(47-var whitelist)], off-site S3 only; a `documenso` command plugin adds the ENCRYPTION_KEY restore gate + `restore-env` lost-key recovery; two-service split (one-shot + scheduler) kept |
| medium | `Container-Solution/NocoDB` | ✅ done — source=[postgres(plain), filesystem:/nocodb-data, **nocodb-rest**]; the REST exporter + restore-schema/records/attachments ship as the `backuphelper-nocodb` plugin (Source + `nocodb` command group) — needs engine ≥ v1.5.0 (CLI-command injection + per-source `enabled`) |
| medium | `Container-Solution/WordPressStack` | ✅ done — pure config: source=[mariadb:database, filesystem:uploads, filesystem:content(subdirs plugins/themes/languages)]; Teams MessageCard kept; no plugin |
| high | `Container-Solution/n8n` | +nodejs/npm/n8n; n8n-CLI source plugin |
| high | `Internal-Projects/BAUERGROUP.HardwareIDAllocator` | .NET→Python re-platform or NDJSON mode |

> **Out of scope** — not migrated onto BackupHelper, these stay standalone:
> `Internal-Projects/CanvaBackupRunner` (standalone SaaS-export runner),
> `Container-Solution/GitHubBackup` (a GitHub-specific git/LFS/wiki engine, not a
> generic DB/file backup), and the Home Assistant `S3CompatibleBackup` (an HA
> plugin, not a container).

Start with the trivial/low tier (pure DB backups) to prove the model, then the medium tier (DB + files/objects), and finally the high tier where an app-specific exporter becomes a plugin.

## What gains you get for free

Repos that migrate inherit capabilities their old sidecar lacked: a sha256 integrity manifest + `verify`, normalized HMAC-SHA256 webhook signing, retry/backoff on network calls, optional client-side encryption, count/age/GFS/smart retention, a functional healthcheck, and the full restore CLI.
