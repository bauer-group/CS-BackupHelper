Complete reference for the `backuphelper` command-line interface — the single entrypoint that runs the scheduler daemon, one-shot backups, and every maintenance/restore subcommand.

## Run modes

The container entrypoint is `backuphelper` (`ENTRYPOINT ["/sbin/tini", "--", "backuphelper"]`). It has three mutually exclusive modes, selected purely by the arguments you pass:

| Mode | Invocation | Behaviour |
| ---- | ---------- | --------- |
| **Daemon** (default) | `backuphelper` (no args) | Starts a blocking APScheduler. Each job runs on its own `cron` trigger; jobs with `schedule.on_startup` also fire once at boot. Runs until the process is signalled. This is what `restart: unless-stopped` keeps alive. |
| **One-shot** | `backuphelper --now` | Runs **every** configured job exactly once, then exits. Exit `0` if all jobs succeeded (or degraded to `warning`), `1` if any job ended in `error`. |
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
| `BACKUP_DATA_DIR` | `/data` | all commands | Directory holding snapshot artifacts (`<id>.tar.gz[.age\|.gpg]`) and sidecar manifests (`<id>.manifest.json`). |
| `TZ` | `Etc/UTC` | daemon | Timezone for cron scheduling. |
| `BACKUP_LOG_LEVEL` | `INFO` | daemon / `--now` | Log verbosity. |
| `BACKUP_LOG_FORMAT` | `console` | daemon / `--now` | `console` or structured JSON logging. |
| `BACKUP_HEALTHCHECK_MAX_AGE_HOURS` | `26` | `healthcheck` | Age threshold for the freshness probe. |

Config loading is uniform: the commands that need the job definition (`create`, `restore`, `prune`, `config`, and the daemon/`--now` modes) all build it through the same layered loader — discrete `BACKUP_<PATH>__…` overrides on top of inline `BACKUP_CONFIG_JSON` / `BACKUP_CONFIG_JSON_BASE64` on top of a mounted `BACKUP_CONFIG_FILE`, with `${VAR}` placeholders interpolated from the environment. See [configuration](configuration.md) for the full precedence rules and [sources](sources.md) for per-source keys. The snapshot-only commands (`list`, `show`, `verify`, `download`, `healthcheck`) read the data dir directly and need no job config.

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

Exit codes: daemon runs until signalled; `--now` returns `0` (all jobs succeeded/warned) or `1` (at least one job errored).

### `create`

Runs every configured job once, now. Functionally identical to `--now` — a subcommand alias for the same one-shot run.

```bash
docker run --rm --env-file .env -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest create
docker compose run --rm backup create
```

Exit codes: `0` all jobs OK/warning · `1` any job errored.

### `list`

Lists local snapshots discovered in the data dir. Each row is the snapshot id and the archive size in bytes; prints `no snapshots found` when the data dir is empty.

```bash
docker run --rm -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest list
docker compose run --rm backup list
```

Exit codes: `0`.

### `show`

Prints the sidecar manifest (`<id>.manifest.json`) for one snapshot — the component list, sizes, per-component sha256, `total_bytes`, `created_at`, and the `archive_sha256` used by `verify`.

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
| `--job <name>` | Select which configured job's sources to restore into. Defaults to the first job. |
| `--only <component>` | Restore only the named component(s); repeatable. Component names are those shown in the manifest (e.g. `database`, `uploads`, `s3`). |

```bash
# Restore everything for the (single) configured job, no prompt
docker run --rm --env-file .env -v backup-data:/data \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest restore 2026-07-05_03-15-00 --force
docker compose run --rm backup restore 2026-07-05_03-15-00 --force

# Restore only the filesystem 'uploads' component of a named job
docker compose run --rm backup \
  restore 2026-07-05_03-15-00 --job main --only uploads --force
```

Exit codes: `0` restore completed (or aborted at the confirmation prompt) · `1` no matching job, or restore finished with per-component errors.

### `prune`

Applies retention to the **local** snapshots in the data dir, deleting all files (`<id>.*`) of each pruned snapshot. Uses the first job's `retention` policy unless overridden.

| Option | Description |
| ------ | ----------- |
| `--keep <n>` | Override the retention `count` with `n` newest to keep. |
| `--dry-run` | Print what would be pruned without deleting anything. |

```bash
# Preview retention on local snapshots
docker compose run --rm backup prune --dry-run

# Keep only the 7 newest, deleting the rest
docker compose run --rm backup prune --keep 7
```

Prints `no jobs configured` when no job (and therefore no retention policy) exists. Exit codes: `0`.

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

Prints the fully-merged effective configuration as JSON, after all layers and `${VAR}` interpolation are resolved — the fastest way to confirm what the engine actually sees.

| Option / Argument | Description |
| ----------------- | ----------- |
| `action` | Positional, defaults to `print`. The command always prints the effective config. |
| `--redacted` | Mask secrets (passwords, keys, tokens) in the output. Use this before sharing config in a ticket or log. |

```bash
docker compose run --rm backup config
docker compose run --rm backup config --redacted
```

Exit codes: `0`.

### `healthcheck`

The container `HEALTHCHECK` probe. Reads the newest sidecar manifest's `created_at` and reports healthy if it is within `BACKUP_HEALTHCHECK_MAX_AGE_HOURS`. A data dir with no manifests is treated as healthy (grace period for a freshly started daemon).

```bash
docker compose run --rm backup healthcheck
```

Exit codes: `0` last backup is fresh (or none yet) · `1` last backup is stale.

## Exit codes at a glance

| Command | 0 | 1 | 2 |
| ------- | - | - | - |
| `--now` / `create` | all jobs OK/warning | any job errored | — |
| `list` | always | — | — |
| `show` | printed | not found | — |
| `verify` | matches manifest | — | mismatch / missing |
| `restore` | completed or aborted | no job / restore errors | — |
| `download` | copied | not found | — |
| `prune` | always | — | — |
| `config` | always | — | — |
| `healthcheck` | fresh / none yet | stale | — |
