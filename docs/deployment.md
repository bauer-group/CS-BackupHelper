Operating the central BackupHelper image: run modes, the `/data` volume, the functional healthcheck, the non-root security posture, and the meta-Dockerfile pattern that consuming repos ship.

## Run modes

The image entrypoint is `backuphelper` (wrapped by `tini` as PID 1). What it does
depends on the argument:

| Invocation | Behaviour |
| --- | --- |
| _(no args)_ | **Scheduler daemon** — a blocking `apscheduler` loop that runs every configured job on its `cron` / `interval` trigger and stays up. This is the default `ENTRYPOINT` behaviour. |
| `--now` | **One-shot** — runs every job once and exits. Exit code `1` if any job ended in `error`, else `0`. |
| `create` | Snapshot every job once now (same as `--now`). |
| `list` / `show <id>` / `verify <id>` | Inspect local snapshots. |
| `restore <id>` | Restore a snapshot (destructive; `--force` skips the confirm prompt). |
| `prune` | Apply retention to local snapshots (`--dry-run`, `--keep N`). |
| `download <id> <dir>` | Copy a snapshot's archive + manifest out of `/data`. |
| `config [print] [--redacted]` | Print the fully-merged effective config, secrets masked with `--redacted`. |
| `healthcheck` | Exit `0` if the last backup is fresh (see below). |

### Daemon vs one-shot deployment

- **Daemon** — a long-lived sidecar with `restart: unless-stopped`. The container
  owns its own schedule (`schedule.mode` = `cron` or `interval`); no host cron
  needed. The Docker `HEALTHCHECK` then reflects backup freshness.
- **One-shot** — invoke with `--now` from an external scheduler (host cron,
  Kubernetes `CronJob`, CI). Use `docker compose run --rm backup --now` so the
  container is not restarted after it exits.

## The `/data` volume

The image declares `VOLUME ["/data"]` and sets `BACKUP_DATA_DIR=/data`. This is
the working/staging store and the local snapshot destination. Always mount a
named volume or bind mount here so snapshots survive container recreation:

```yaml
volumes:
  - backup-data:/data
```

Layout inside `/data`:

- `<snapshot-id>.tar.gz` (or `.tar.gz.age` / `.tar.gz.gpg` when encrypted) — the bundle
- `<snapshot-id>.manifest.json` — the sidecar manifest carrying `created_at`,
  per-component `sha256`, and `archive_sha256`
- `.work/<snapshot-id>/` — transient staging, removed after each run

`BACKUP_DATA_DIR` is overridable if you need a different mount path. The `local`
destination is always present as the staging store; a `keep-local` policy governs
whether the local copy survives once an `s3` destination has the off-site copy.

## The functional healthcheck

The image ships a **functional** healthcheck — it reports on backup staleness,
not just process liveness:

```dockerfile
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD backuphelper healthcheck || exit 1
```

`backuphelper healthcheck` reads the newest `*.manifest.json` in `/data`, parses
its `created_at`, and exits `0` when that is within `BACKUP_HEALTHCHECK_MAX_AGE_HOURS`
(default **26** — one daily run plus a grace margin), else exits `1`.

- A **missing** manifest is treated as healthy (grace), so a freshly started
  daemon that has not run yet is not reported unhealthy.
- Tune the window per deployment, e.g. for a job that runs every 6 hours:

  ```yaml
  environment:
    BACKUP_HEALTHCHECK_MAX_AGE_HOURS: "8"
  ```

Because the probe turns "no backup in N hours" into an unhealthy container, it
composes with orchestrator restart/alert policies and with the `healthchecks`
notification channel.

> The base image also installs `procps` (providing `pgrep`) if you prefer to add
> a pure-liveness probe alongside the functional one.

## Security posture

The runtime is deliberately minimal and unprivileged:

- **Non-root** — runs as user/group `backup` (uid/gid **1000**). `/data` is
  `chown`ed to `backup` at build time.
- **`tini` as PID 1** — `ENTRYPOINT ["/sbin/tini", "--", "backuphelper"]` reaps
  zombies and forwards signals for clean shutdown of the scheduler.
- **Small base** — `python:3.14-alpine` with only the needed runtime packages:
  `postgresql<major>-client`, `mariadb-client`, `gnupg`, `age`, `tini`, `tzdata`,
  `ca-certificates`, `procps`.
- **Test-gated build** — the production stage cannot be assembled unless the
  `pytest` stage passes (`COPY --from=test` creates a hard dependency on the test
  stage). A red test suite means no image.
- **Secrets stay out of the config literal** — reference them as `${ENV_VAR}` in
  the JSON; they are resolved from the environment at load time. Passwords are
  passed to dump tools via the process environment (e.g. `PGPASSWORD`), never on
  the command line, so they do not appear in `ps` output.

Recommended hardening for the compose service (these are deployment conventions,
not baked into the image):

```yaml
services:
  backup:
    read_only: true
    tmpfs:
      - /tmp            # restore extracts to a TemporaryDirectory under /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
```

## GHCR image and version tags

The image is published to GitHub Container Registry:

```
ghcr.io/bauer-group/cs-backuphelper/backuphelper:<tag>
```

Use the tag ladder to pin as loosely or tightly as you want:

| Tag | Tracks |
| --- | --- |
| `latest` | newest release (fine for dev, avoid for prod) |
| `1` | the `1.x` line — picks up minor + patch releases |
| `1.2` | the `1.2.x` line — picks up patches only |
| `1.2.3` | one exact release |

Meta-Dockerfiles should pin to a major (`:1`) so security/patch fixes flow in
without breaking on a major bump.

## The meta-Dockerfile pattern (key section)

BackupHelper is the **central** image. A consuming repo does **not** fork it —
it ships a thin (~20-line) meta-Dockerfile that only:

1. inherits `FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:1`,
2. sets its own OCI labels (provenance for the repo's derived image),
3. optionally adds extra clients its sources need,

and gets its sources/destinations/schedule entirely from environment or
`BACKUP_CONFIG_JSON` in compose — **no config baked into the image**.

```dockerfile
# syntax=docker/dockerfile:1
# MyApp backup image — thin meta-layer over the central BackupHelper engine.
FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:1

# OCI provenance for THIS repo's derived image.
LABEL org.opencontainers.image.title="MyApp Backup"
LABEL org.opencontainers.image.description="Backup sidecar for MyApp — BackupHelper meta-layer"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/MyApp"
LABEL org.opencontainers.image.licenses="MIT"

# OPTIONAL: add an app-specific client the base does not carry.
# USER root
# RUN apk add --no-cache redis
# USER backup

# Sources / destinations / schedule come from env or BACKUP_CONFIG_JSON
# in docker-compose — nothing app-specific is baked into this image.
```

To add a **Source plugin** (n8n CLI export, NocoDB REST export, …) instead of an
extra client, `pip install` the plugin package in this same meta-layer — see
[plugins.md](./plugins.md) for the complete example.

### Pinning the PostgreSQL client major

`PG_CLIENT_VERSION` is a **build-arg of the central image** (default `18`), which
selects `postgresql${PG_CLIENT_VERSION}-client`. It is baked into the base tag you
inherit — a bare `ARG` in a meta-layer does not repin the inherited package. To
run a different major you either:

- select a base image tag that was built with that major, or
- build the engine from source with the arg:

  ```bash
  docker build --build-arg PG_CLIENT_VERSION=17 -t myregistry/backuphelper:17 .
  ```

The bundled `mariadb-client` covers MariaDB 11/12 and MySQL 8/9, so no analogous
pin is needed for those.

## Compose: the `backup` service and profile pattern

The shipped [`docker-compose.yml`](../docker-compose.yml) defines a `backup`
service alongside the app, config supplied inline via `BACKUP_CONFIG_JSON` with
`${VAR}` placeholders for secrets. Key deployment knobs:

- **Restart policy** — `restart: unless-stopped` for the daemon; drop it and use
  `docker compose run --rm backup --now` for one-shot runs.
- **Resource limits** — cap the sidecar so a large dump cannot starve the app:

  ```yaml
  services:
    backup:
      deploy:
        resources:
          limits:
            cpus: "1.0"
            memory: 512M
  ```

- **`backup` compose profile** — put the sidecar behind a profile so it only
  starts when explicitly requested, keeping the default `up` lean:

  ```yaml
  services:
    backup:
      profiles: ["backup"]
      image: ghcr.io/bauer-group/cs-backuphelper/backuphelper:1
      # ...
  ```

  ```bash
  docker compose --profile backup up -d       # run the daemon sidecar
  docker compose --profile backup run --rm backup --now      # one-shot snapshot
  docker compose --profile backup run --rm backup verify <snapshot-id>
  ```

## See also

- [plugins.md](./plugins.md) — writing Source plugins and lifecycle hooks.
- [migration.md](./migration.md) — adopting BackupHelper across the fleet.
- [../README.md](../README.md) — configuration layers, sources table, CLI reference.
