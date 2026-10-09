Operating the central BackupHelper image: run modes, the `/data` volume, the functional healthcheck, the non-root security posture, and the meta-Dockerfile pattern that consuming repos ship.

## Run modes

The image entrypoint is `backuphelper` (wrapped by `tini` as PID 1). What it does
depends on the argument:

| Invocation | Behaviour |
| --- | --- |
| _(no args)_ | **Scheduler daemon** — a blocking `apscheduler` loop that runs every configured job on its `cron` / `interval` trigger and stays up. This is the default `ENTRYPOINT` behaviour. |
| `--now` | **One-shot** — runs every job once and exits. Exit code `1` if any job ended in `error` (a failed component, a snapshot stored nowhere, an aborted run — see [run status](cli.md#run-status)), else `0`. |
| `create` | Snapshot every job once now (same as `--now`). |
| `list` / `show <id>` / `verify <id>` | Inspect local snapshots. |
| `restore <id>` | Restore a snapshot (destructive; `--force` skips the confirm prompt). |
| `prune` | Apply every job's retention to its own local snapshots (`--job NAME`, `--dry-run`, `--keep N`). |
| `download <id> <dir>` | Copy a snapshot's archive + manifest out of `/data`. |
| `config [print] [--show-secrets]` | Print the fully-merged effective config; secrets are masked unless `--show-secrets` is passed. |
| `healthcheck` | Exit `0` if backups work (see below). |

### Daemon vs one-shot deployment

- **Daemon** — a long-lived sidecar with `restart: unless-stopped`. The container
  owns its own schedule (`schedule.mode` = `cron` or `interval`); no host cron
  needed. The Docker `HEALTHCHECK` then reflects whether backups work.
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

- `<snapshot-id>.tar.gz` (or `.tar.gz.age` / `.tar.gz.gpg` when encrypted) — the bundle.
  The id is the run's UTC start, `2026-07-05_03-15-00`; in a config with several
  jobs it also names the job, `2026-07-05_03-15-00_files-nightly` (see
  [snapshot ids](configuration.md#snapshot-ids))
- `<snapshot-id>.manifest.json` — the sidecar manifest carrying `created_at`,
  per-component `sha256`, and `archive_sha256`
- `.work/<snapshot-id>/` — transient staging, removed after each run
- `.state/` — run records for the [healthcheck](#the-functional-healthcheck)
  (since 1.7.7): `daemon.json` (when the daemon started) and one
  `job-<job>.json` per job (its last run). They are not snapshots: `list`,
  `prune` and retention ignore them. Leave them in place — without them a
  `keep_local: false` job is unhealthy until its next run.

`BACKUP_DATA_DIR` is overridable if you need a different mount path. The `local`
destination is always present as the staging store; a `keep-local` policy governs
whether the local copy survives once an `s3` destination has the off-site copy.

## The functional healthcheck

The image ships a **functional** healthcheck — it reports whether backups work,
not just whether the process lives:

```dockerfile
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD backuphelper healthcheck || exit 1
```

`backuphelper healthcheck` exits `1` (unhealthy) when — checked in this order:

1. **The data dir cannot take a backup.** `/data` is missing, or it or
   `/data/.state` is not writable by the user the container runs as (uid 1000
   `backup` unless an image changes it). Typical cause: a bind mount or an old
   volume owned by another uid — fix it with a one-time `chown`.
2. **The most recent run failed.** It ended in `error` (see
   [run status](cli.md#run-status)) or its snapshot has a failed component. The
   check stays unhealthy until a newer run ends in `success` or `warning`.
   Warnings — an unreachable S3 destination while the local copy exists,
   skipped unreadable files, the encryption fallback — are not failures.
3. **The most recent run is stale.** It started more than the max age ago:
   the job's `healthcheck_max_age_hours`, else
   `BACKUP_HEALTHCHECK_MAX_AGE_HOURS` (default **26** — one daily run plus a
   margin).
4. **No backup has run yet and the grace is over.** A daemon that has not run a
   backup yet is healthy for the max age after its start, unhealthy afterwards.
   Without a recorded daemon start — e.g.
   `docker compose run --rm backup healthcheck` on a volume no daemon has used —
   it is unhealthy right away.

With several jobs, rules 2–4 apply to every configured job on its own (see
[below](#where-the-most-recent-run-comes-from)), and the check is unhealthy as
soon as one job is.

Otherwise it exits `0`. Either way it prints one line with the verdict and the
reason, which `docker inspect --format '{{json .State.Health}}' <container>`
shows:

```text
unhealthy: the last backup failed: snapshot 2026-07-05_03-15-00 (job main) at 2026-07-05T03:15:00+00:00: failed component(s): database
```

With several jobs the line has one part per job (`job <name>: <reason>`,
separated by `;`) — only the unhealthy jobs when it is unhealthy, every job
when it is healthy.

The check only reads: it writes nothing and opens no network connection.

### Where "the most recent run" comes from

- **Run records** (since 1.7.7) — every run, an aborted one included, writes
  `/data/.state/job-<job>.json` with the job, snapshot id, status, start time
  and failed components; the daemon writes `/data/.state/daemon.json` with its
  start time before any job runs. Writes are atomic. A record that cannot be
  written is logged (`could not record the outcome of job …`) and never fails
  the run; the check then judges by the previous record and the manifests and
  turns unhealthy once they are stale.
- **Sidecar manifests** — every `<id>.manifest.json` still in `/data`. A
  snapshot counts as failed when its `status` is `error` or a component has an
  `error`; manifests written before 1.7.7 have no `status` and count by their
  components.

The newest of both by start time decides; on equal times the run record wins.

- **One job** (or no loadable config): every record and manifest in the data
  dir counts, as up to 1.7.7.
- **Several jobs**: each configured job is judged by its own run record and the
  manifests of its own snapshots — a [job-scoped id](configuration.md#snapshot-ids)
  names its job; a plain manifest from before job-scoped ids counts for the job
  that owns it (see [retention per job](retention.md#retention-applies-per-job-and-destination)).
  A failed or stale job keeps the container unhealthy until that job runs
  successfully again, whatever the other jobs do. Up to 1.7.7 the newest run of
  any job decided, so another job's newer good run hid the failure. Run records
  of a job that is no longer configured are ignored.

The check loads the job config (like the daemon) for the job names and their
max ages. If it cannot — the config is invalid, so the daemon cannot start with
it either — it judges the data dir as a whole with
`BACKUP_HEALTHCHECK_MAX_AGE_HOURS` and notes the config error on stderr.

`keep_local: false` and S3-only jobs leave no local manifest after a successful
upload; their run records keep them monitored, so a job that stops running turns
the check unhealthy after its max age. During an S3 outage
their runs end in `warning` and keep a fallback copy in `/data`, so the check
stays healthy; once S3 is back the copies are uploaded and removed (see
[destinations](destinations.md#catching-up-after-an-s3-outage)).

### Choosing `BACKUP_HEALTHCHECK_MAX_AGE_HOURS`

The max age is both the staleness limit and the grace after a daemon start, so
set it above the longest gap between two scheduled runs plus a margin for the
run itself. `BACKUP_HEALTHCHECK_MAX_AGE_HOURS` sets it for every job; a job's
`healthcheck_max_age_hours` overrides it for that job — e.g. a weekly job next
to hourly ones:

| Schedule | `BACKUP_HEALTHCHECK_MAX_AGE_HOURS` / `healthcheck_max_age_hours` |
| --- | --- |
| every 6 hours | `8` |
| daily (the default `15 3 * * *`) | `26` (default) |
| weekly (`day_of_week` set to one day, or `interval_hours: 168`) | `170` or more |

```yaml
environment:
  BACKUP_HEALTHCHECK_MAX_AGE_HOURS: "170"   # weekly schedule
```

Per job (sources and destinations left out):

```json
{"jobs": [
  {"name": "database-hourly", "schedule": {"mode": "interval", "interval_hours": 1},
   "healthcheck_max_age_hours": 3},
  {"name": "files-weekly", "schedule": {"cron": "0 2 * * 0"},
   "healthcheck_max_age_hours": 170}
]}
```

With the default `26`, a weekly job turns unhealthy one day after every run and
one day after a fresh start before its first run.

### A fresh stack

The daemon records its start before the first probe runs (the image probes after
60 s), so a freshly started stack on an empty volume is healthy —
`docker compose up --wait` and the backup round-trip CI rely on that. This is
the grace from rule 4, not a free pass: if no backup has run within
`BACKUP_HEALTHCHECK_MAX_AGE_HOURS`, the container turns unhealthy. A restarted
stack is judged by its last run as before: if that failed or is stale, the
container is unhealthy right after the start.

Because the probe turns "backups are not working" into an unhealthy container,
it composes with orchestrator restart/alert policies and with the `healthchecks`
notification channel.

> The base image also installs `procps` (providing `pgrep`) if you prefer to add
> a pure-liveness probe alongside the functional one.

## Behaviour changes in 1.7.7

1.7.7 makes the run status, the exit code and the healthcheck report failed
components and missing backups. Check these before rolling it out:

- **A failed component is an error.** A run in which one source failed
  completely now ends in `error` instead of `warning`: `--now` / `create` exit
  `1`, and the alert is delivered at error level — also with `level: errors`;
  the `healthchecks` channel pings `/fail`. Wrappers and CI that ran `create`
  and accepted a partial snapshot with exit `0` now fail.
- **A deployment that keeps producing partial snapshots turns unhealthy.** If
  the newest snapshot has a failed component — also one written by 1.7.6 — the
  container is unhealthy right after the upgrade, until a complete snapshot
  exists. Fix the failing source, or disable it with `"enabled": false`.
- **"No backup yet" is no longer healthy forever.** It is healthy for
  `BACKUP_HEALTHCHECK_MAX_AGE_HOURS` after the daemon start. Weekly schedules
  need a value above 168, see
  [Choosing `BACKUP_HEALTHCHECK_MAX_AGE_HOURS`](#choosing-backup_healthcheck_max_age_hours).
- **`keep_local: false` and S3-only jobs are monitored.** Up to 1.7.6 the probe
  stayed in its grace for them; now a job that stops running turns the container
  unhealthy after `BACKUP_HEALTHCHECK_MAX_AGE_HOURS`.
- **An unwritable data dir is unhealthy** right away
  ([rule 1](#the-functional-healthcheck)). An image that
  added its own `[ -w /data ]` guard to the `HEALTHCHECK` can keep it; it is now
  redundant.
- **New entries:** the `/data/.state/` records, the manifest field `status`
  (printed by `show`), the one-line healthcheck reason, and the alert message
  `snapshot completed with warnings` (warnings used to read `snapshot completed
  with errors`).

## Behaviour changes after 1.7.7

A config with **one job** behaves as before — same snapshot ids, retention,
`prune` and healthcheck — except for the first point:

- **Discrete env overrides are typed by their field**
  ([details](configuration.md#discrete-env-overrides)). A text field takes the
  value verbatim, so `…__PASSWORD=20261006` now works instead of failing
  validation. A source's or destination's own key gets `true` / `false` /
  `null` and JSON arrays / objects parsed, numbers stay text; every built-in
  source and the S3 destination convert them. A plugin source that reads a
  number from its spec without a pydantic model now gets text when the value
  comes from a discrete override.

For a config with **several jobs**:

- **Snapshot ids name the job**, e.g. `2026-07-05_03-15-00_files-nightly`
  ([snapshot ids](configuration.md#snapshot-ids)). Scripts that match ids
  with `^YYYY-MM-DD_HH-MM-SS$` must accept the `_<job>` suffix. Snapshots from
  before the upgrade keep their plain ids and stay listable, verifiable,
  restorable and prunable.
- **Retention and `prune` work per job**: a job prunes only its own snapshots,
  on the data dir and on S3 ([retention per job](retention.md#retention-applies-per-job-and-destination)).
  Old plain snapshots count for the first job that stores in that place (a
  fallback copy for the job its pending marker names); those of a job that is
  no longer configured are never pruned — remove them by hand.
  `prune` applies every job's own policy, `prune --job NAME` one job's.
- **The healthcheck judges every job on its own**, with the job's
  `healthcheck_max_age_hours` when set. A failed or stale job keeps the
  container unhealthy until it runs successfully again, also when another job
  ran later. A job that has never run turns unhealthy once the grace after the
  daemon start is over — check that every configured job is scheduled to run
  within its max age.

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

1. inherits `FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest`,
2. sets its own OCI labels (provenance for the repo's derived image),
3. optionally adds extra clients its sources need,

and gets its sources/destinations/schedule entirely from environment or
`BACKUP_CONFIG_JSON` in compose — **no config baked into the image**.

```dockerfile
# syntax=docker/dockerfile:1
# MyApp backup image — thin meta-layer over the central BackupHelper engine.
FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest

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
      image: ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest
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
