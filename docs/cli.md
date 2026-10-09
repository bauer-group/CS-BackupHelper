Complete reference for the `backuphelper` command-line interface — the single entrypoint that runs the scheduler daemon, one-shot backups, and every maintenance/restore subcommand.

## Run modes

The container entrypoint is `backuphelper` (`ENTRYPOINT ["/sbin/tini", "--", "backuphelper"]`). It has three mutually exclusive modes, selected purely by the arguments you pass:

| Mode | Invocation | Behaviour |
| ---- | ---------- | --------- |
| **Daemon** (default) | `backuphelper` (no args) | Starts a blocking APScheduler. Each job runs on its own `cron` trigger; jobs with `schedule.on_startup` also fire once at boot. Runs until the process is signalled. This is what `restart: unless-stopped` keeps alive. |
| **One-shot** | `backuphelper --now` | Runs **every** configured job exactly once, then exits. Exit `0` if every job ended in `success` or `warning`, `1` if any job ended in `error` — see [run status](#run-status). |
| **Subcommand** | `backuphelper <command> …` | Runs a single maintenance/restore command (`create`, `list`, `show`, `verify`, `restore`, `prune`, `download`, `config`, `healthcheck`) and exits. |

## Invocation forms

Every example below is shown twice. The two forms are equivalent — the compose service already carries the config and volumes, so it is the shorter one for day-to-day use.

```bash
# Raw docker: pass the same env + data volume the daemon uses
docker run --rm \
  --env-file .env \
  -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest <command> [args]

# docker compose: reuse the 'backup' service definition as-is
docker compose run --rm backup <command> [args]
```

Because arguments are appended after the `backuphelper` entrypoint, `docker run … <image> list` becomes `backuphelper list` inside the container.

## Environment

| Variable | Default | Used by | Purpose |
| -------- | ------- | ------- | ------- |
| `BACKUP_DATA_DIR` | `/data` | all commands | Directory holding snapshot artifacts (`<id>.tar.gz[.age\|.gpg]`) and sidecar manifests (`<id>.manifest.json`). An id is the run's UTC start, `2026-07-05_03-15-00`; with several jobs it also names the job, `2026-07-05_03-15-00_files-nightly` — see [snapshot ids](configuration.md#snapshot-ids). |
| `TZ` | `Etc/UTC` | daemon | Timezone for cron scheduling. |
| `BACKUP_LOG_LEVEL` | `INFO` | daemon / `--now` | Log verbosity. |
| `BACKUP_LOG_FORMAT` | `console` | daemon / `--now` | `console` or structured JSON logging. |
| `BACKUP_HEALTHCHECK_MAX_AGE_HOURS` | `26` | `healthcheck` | Maximum age of the last run, and the grace after the daemon start while no backup has run yet. Set it above the longest gap between two scheduled runs — e.g. `170` for a weekly schedule, see [deployment](deployment.md#choosing-backup_healthcheck_max_age_hours). |

Config loading is uniform: the commands that need the job definition (`create`, `restore`, `prune`, `config`, and the daemon/`--now` modes) all build it through the same layered loader — discrete `BACKUP_<PATH>__…` overrides on top of inline `BACKUP_CONFIG_JSON` / `BACKUP_CONFIG_JSON_BASE64` on top of a mounted `BACKUP_CONFIG_FILE`, with `${VAR}` placeholders interpolated from the environment. See [configuration](configuration.md) for the full precedence rules and [sources](sources.md) for per-source keys. The snapshot-only commands (`list`, `show`, `verify`, `download`, `healthcheck`) read the data dir directly and need no job config.

## Run status

Every run of a job ends in one of three statuses. The status decides the exit code of `--now` / `create`, the alert and what the healthcheck reports. **Changed in 1.7.7:** a component that failed completely makes the whole run `error`. Up to 1.7.6 the run only degraded to `warning` as long as another component succeeded.

| Status | When | Examples | `--now` / `create` | Alert | Healthcheck |
| ------ | ---- | -------- | ------------------ | ----- | ----------- |
| `success` | Every component was backed up and the snapshot was stored on every destination. | — | exit `0` | delivered only at `level: all` (title `backup success`) | healthy while fresh |
| `warning` | Every component was backed up and the snapshot is stored, but something non-fatal went wrong. | a filesystem source skipped unreadable files (`metadata.warnings`) · an S3 destination is unreachable or its upload failed while the local copy exists (or a fallback copy was kept in the data dir) · `keep_local: false` kept the local copy because no off-site copy exists · encryption failed and the snapshot was stored unencrypted · retention failed | exit `0` | delivered at `level: warnings` (default) and `all` | healthy while fresh |
| `error` | A component failed completely, the snapshot was stored on no destination, or the run aborted. | `pg_dump` / `mariadb-dump` / `mysqldump` failed · a plugin source raised · an S3 *source* failed · a filesystem `path` is missing or unreadable · a source returned no output · the local and the off-site put both failed · a `pre_backup` hook raised | exit `1` | delivered at every level (`errors`, `warnings`, `all`); the [healthchecks](notifications.md#healthchecks-dead-mans-switch) channel pings `/fail` | unhealthy until a newer run ends in `success` or `warning` |

- A snapshot with a failed component is still stored, listed and verifiable, and its good components stay restorable (`restore <id> --only <name>`). The failed component holds no data and is listed in the manifest with size `0`, an empty `sha256` and its `error` text.
- Disabled sources (`"enabled": false`) and S3 sources without a `bucket` produce no component and never affect the status.
- An aborted run (an exception before the run finished, e.g. a raising `pre_backup` hook or a full disk while bundling) sends an `error` alert and ends `--now` / `create` with exit `1`; jobs listed after it in the same invocation do not run.
- The alert message is a fixed text per outcome — `snapshot completed`, `snapshot completed with warnings`, `snapshot is incomplete - a component failed`, `snapshot was not stored on any destination`, `run aborted before it finished`. The details are in the alert's error list.

The sidecar manifest records the status of the snapshot's **content** as `status`: `error` when a component failed, `warning` when a component reported `metadata.warnings`, else `success`. Destination, encryption, retention and `keep_local` problems happen after the manifest is written, so they show up only in the run status (exit code, alert, healthcheck), not in the manifest. Manifests written before 1.7.7 have no `status` field.

## Commands

### `backuphelper` — daemon / `--now`

The default callback. With no subcommand it loads config, configures logging, and either runs the scheduler daemon or, with `--now`, runs all jobs once.

| Option | Description |
| ------ | ----------- |
| `--now` | Run every job once and exit instead of starting the daemon. |

```bash
# Start the scheduler (this is the container's default CMD)
docker run --rm --env-file .env -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest
docker compose up -d backup

# Force one immediate run of all jobs, then exit
docker run --rm --env-file .env -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest --now
docker compose run --rm backup --now
```

Exit codes: daemon runs until signalled; `--now` returns `0` (every job ended in `success` or `warning`) or `1` (at least one job ended in `error` — a failed component, a snapshot stored nowhere or an aborted run). See [run status](#run-status).

### `create`

Runs every configured job once, now. Functionally identical to `--now` — a subcommand alias for the same one-shot run.

```bash
docker run --rm --env-file .env -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest create
docker compose run --rm backup create
```

Exit codes: `0` every job ended in `success` or `warning` · `1` any job ended in `error` (see [run status](#run-status)).

### `list`

Lists local snapshots discovered in the data dir — of every job — plus those that exist only in the off-site S3 target of the job selected with `--job` (default: the first job), marked `(off-site only)` with size `0`. Each row is the snapshot id and the archive size in bytes, sorted by time; prints `no snapshots found` when there is none. Plain and job-scoped ids ([snapshot ids](configuration.md#snapshot-ids)) appear side by side.

```bash
docker run --rm -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest list
docker compose run --rm backup list
```

Exit codes: `0`.

### `show`

Prints the sidecar manifest (`<id>.manifest.json`) for one snapshot — the component list, sizes, per-component sha256, `total_bytes`, `created_at`, the snapshot `status` (since 1.7.7, see [run status](#run-status)) and the `archive_sha256` used by `verify`. A source that failed during the backup is listed too, with size `0`, an empty `sha256` and its `error` text.

| Argument | Description |
| -------- | ----------- |
| `snapshot_id` | The snapshot id (as shown by `list`). |

```bash
docker run --rm -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest show 2026-07-05_03-15-00
docker compose run --rm backup show 2026-07-05_03-15-00
```

Exit codes: `0` printed · `1` snapshot not found.

### `verify`

Recomputes the archive's sha256 and compares it against `archive_sha256` in the sidecar manifest. This is the integrity gate you run before restoring. Prints `OK <id>` or `FAILED <id>`.

| Argument | Description |
| -------- | ----------- |
| `snapshot_id` | The snapshot id to check. |
| `--job <name>` | The job whose off-site S3 target holds the snapshot when it is not in the data dir. Defaults to the job a job-scoped id names, else the first job. |

```bash
docker run --rm -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest verify 2026-07-05_03-15-00
docker compose run --rm backup verify 2026-07-05_03-15-00
```

Exit codes: `0` archive matches manifest · `2` mismatch, missing archive, or missing/empty manifest hash.

### `restore`

**DESTRUCTIVE.** Decrypts (if needed), extracts, and replays a snapshot onto the live sources. Full walkthrough and per-source behaviour in [restore](restore.md).

| Option / Argument | Description |
| ----------------- | ----------- |
| `snapshot_id` | The snapshot id to restore. |
| `--force`, `-f` | Skip the interactive "this overwrites live data" confirmation. Required for non-interactive runs. |
| `--job <name>` | Select which configured job's sources to restore into. Defaults to the job a job-scoped id names (`…_files-nightly` → `files-nightly`), else the first job. |
| `--only <component>` | Restore only the named component(s); repeatable. Component names are those shown in the manifest (e.g. `database`, `uploads`, `s3`). Every name is checked **before anything is touched**: a name that is not in the snapshot, that failed at backup time, or that has no matching source in the selected job aborts the restore with exit `1` and logs the valid component names. |

```bash
# Restore everything for the (single) configured job, no prompt
docker run --rm --env-file .env -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest restore 2026-07-05_03-15-00 --force
docker compose run --rm backup restore 2026-07-05_03-15-00 --force

# Restore only the filesystem 'uploads' component of a named job
docker compose run --rm backup \
  restore 2026-07-05_03-15-00 --job main --only uploads --force
```

Exit codes: `0` restore completed (or aborted at the confirmation prompt) · `1` no matching job, snapshot missing or corrupt, an `--only` name that cannot be restored (nothing is touched), or restore finished with per-component errors.

### `prune`

Applies retention to the **local** snapshots in the data dir, deleting all files (`<id>.*`) of each pruned snapshot. Every job's `retention` policy is applied to **that job's own** snapshots only — the ones whose [id](configuration.md#snapshot-ids) names it, plus, in a config with several jobs, plain ids it owns (see [retention per job](retention.md#retention-applies-per-job-and-destination)). A job never prunes another job's snapshot. With a single job this is the same as before: its policy over every snapshot in the data dir.

| Option | Description |
| ------ | ----------- |
| `--job <name>` | Prune only this job's snapshots. Default: every configured job, each by its own policy. |
| `--keep <n>` | Override the retention `count` with `n` newest to keep — per job. |
| `--dry-run` | Print what would be pruned without deleting anything. |

```bash
# Preview retention on local snapshots
docker compose run --rm backup prune --dry-run

# Keep only the 7 newest of every job, deleting the rest
docker compose run --rm backup prune --keep 7

# Only the snapshots of the job "files-nightly"
docker compose run --rm backup prune --job files-nightly --keep 7
```

Prints `no jobs configured` when no job (and therefore no retention policy) exists. Exit codes: `0` · `1` `--job` names no configured job (nothing is deleted). Up to 1.7.7 `prune` applied the first job's policy to every snapshot in the data dir, other jobs' included.

### `download`

Copies a snapshot's archive and sidecar manifest out of the data dir into a target directory — the export step for off-box/off-site storage.

| Argument | Description |
| -------- | ----------- |
| `snapshot_id` | The snapshot id to export. |
| `dest` | Target directory (created if missing). |

```bash
docker run --rm -v backup-data:/data -v "$PWD/export":/export \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest download 2026-07-05_03-15-00 /export
docker compose run --rm -v "$PWD/export":/export backup \
  download 2026-07-05_03-15-00 /export
```

Exit codes: `0` copied · `1` snapshot not found.

### `config`

Prints the fully-merged effective configuration as JSON, after all layers and `${VAR}` interpolation are resolved — the fastest way to confirm what the engine actually sees. Secrets are **redacted by default** (rule below), so the output can go into a ticket.

| Option / Argument | Description |
| ----------------- | ----------- |
| `action` | Positional, defaults to `print`. The command always prints the effective config. |
| `--show-secrets` | Print secrets in cleartext instead of `***`. |
| `--redacted` | Deprecated no-op (redaction is the default); kept so old scripts keep working. |

```bash
docker compose run --rm backup config                  # redacted
docker compose run --rm backup config --show-secrets   # cleartext — do not share
```

Redaction works on the parsed config, so the output stays valid JSON. A value is masked when its key name (case-insensitive) contains `password`, `passwd`, `passphrase`, `secret`, `token`, `credential`, `signature`, `api_key`, `access_key` or `private_key` (with or without separator), ends in a qualified `_key` / `-key` / `.key`, or is `sig` — e.g. `secret_key`, `client_secret`, `smtp_password`, the webhook `secret`, the ntfy `token`, `sse_customer_key`. A notification channel's `url` is cut down to `scheme://host/***`, because for Slack, Discord, Teams and healthchecks the URL itself is the credential, and the ntfy `topic` is masked. Unset values (`null`, `""`) stay visible so you can see whether a secret is configured at all, and credentials embedded in other URLs (`https://user:pass@host`, `?token=…`) are masked too. A bare `key` and hashes such as `sha256` are **not** masked: in this engine `key` is an object-store path, which is diagnostic, not secret. Redaction goes by key name, so a plugin field that holds a secret under an unrelated name (e.g. `dsn`) is only masked where it embeds `user:pass@`. The same rule masks `key=value` / `"key": "value"` pairs in every log line.

Exit codes: `0`.

### `healthcheck`

The container `HEALTHCHECK` probe. It reports whether backups work — not only whether one is recent — and prints one line with its verdict and the reason. It is unhealthy when the data dir is not writable, when the most recent run ended in `error` or left a snapshot with a failed component, when the most recent run is older than `BACKUP_HEALTHCHECK_MAX_AGE_HOURS`, or when no backup has run within that time after the daemon started. It reads the run records in `<data dir>/.state/` and the sidecar manifests, so it also works for `keep_local: false`. The full rules are in [deployment](deployment.md#the-functional-healthcheck).

```bash
docker compose exec backup backuphelper healthcheck
# healthy: the last backup is fresh: snapshot 2026-07-05_03-15-00 (job main) ran 7.2 h ago
# unhealthy: the last backup failed: snapshot 2026-07-05_03-15-00 (job main) at 2026-07-05T03:15:00+00:00: failed component(s): database
```

Run it in the daemon's container (`exec`): a one-off `docker compose run --rm backup healthcheck` against a volume that no daemon has used yet reports `unhealthy: no backup has run yet and no daemon start is recorded`.

Exit codes: `0` healthy · `1` unhealthy.

## Exit codes at a glance

| Command | 0 | 1 | 2 |
| ------- | - | - | - |
| `--now` / `create` | every job `success` / `warning` | any job `error` | — |
| `list` | always | — | — |
| `show` | printed | not found | — |
| `verify` | matches manifest | — | mismatch / missing |
| `restore` | completed or aborted | no job / invalid `--only` / restore errors | — |
| `download` | copied | not found | — |
| `prune` | pruned (or nothing to prune) | `--job` names no job | — |
| `config` | always | — | — |
| `healthcheck` | healthy | last run failed, stale, no backup after the grace, or data dir not writable | — |
