How BackupHelper is configured: the layered loader, secret handling, and the full config schema. For per-topic detail see [sources](sources.md), [destinations](destinations.md), [retention](retention.md), [notifications](notifications.md) and [encryption](encryption.md).

## Configuration layers

Configuration is assembled from four layers. Higher layers override lower ones:

| Precedence | Source | Use for |
| --- | --- | --- |
| 1 (highest) | Discrete env overrides — `BACKUP_<PATH>` with `__` separators | tweaking one leaf per deployment |
| 2 | `BACKUP_CONFIG_JSON` / `BACKUP_CONFIG_JSON_BASE64` | the whole config inline, no host file |
| 3 | `BACKUP_CONFIG_FILE` (a mounted `.json` or `.yaml`) | a mounted config file |
| 4 (lowest) | Built-in model defaults | everything unset |

A file (layer 3) is loaded first, then inline JSON (layer 2) is deep-merged on top, then discrete env overrides (layer 1) are applied. Invalid config fails fast with exit code `2` **before** any network call.

### Inline JSON (no host file)

Pass the entire (multi-job) config as one env var:

```yaml
environment:
  BACKUP_CONFIG_JSON: |
    {"instance_name": "app", "jobs": [ ... ]}
```

For large/nested configs that fight YAML quoting, base64-encode it instead:

```bash
BACKUP_CONFIG_JSON_BASE64=$(base64 -w0 backup.json)
```

### Mounted file

```yaml
environment:
  BACKUP_CONFIG_FILE: /config/backup.json
volumes:
  - ./backup.json:/config/backup.json:ro
```

In Compose you can inline the file content without a host file using a `configs:` block — see [docker-compose.sidecar.yml](../docker-compose.sidecar.yml).

### Discrete env overrides

Any leaf of the config is addressable with `BACKUP_` + the path, `__`-separated, numeric segments indexing arrays:

```bash
BACKUP_JOBS__0__RETENTION__COUNT=30
BACKUP_JOBS__0__SCHEDULE__CRON="0 2 * * *"
```

Values are parsed as JSON when possible (so `30` is an int, `true` a bool), otherwise kept as strings.

## Secrets: `${VAR}` interpolation

Never put a secret literally in the config. Reference an env var instead:

```json
{"type": "postgres", "password": "${DB_PASSWORD}"}
```

`${VAR}` placeholders are resolved recursively from the environment **after** the config is assembled, so the secret lives only in the container's environment, not in the config text. A referenced-but-unset variable is a fatal config error.

In Docker Compose, write `$${VAR}` (doubled `$`) so Compose leaves the placeholder literal for BackupHelper to resolve at runtime rather than substituting it into the rendered file.

Inspect the effective config with secrets masked:

```bash
backuphelper config --redacted
```

## Config schema

```json
{
  "version": 1,
  "instance_name": "app",
  "jobs": [ { <job> } ]
}
```

| Field | Default | Description |
| --- | --- | --- |
| `version` | `1` | Config schema version |
| `instance_name` | `"backup"` | Label stamped into every snapshot, manifest and alert |
| `jobs` | `[]` | One or more backup jobs |

### Job

A container runs **N jobs**; the common case is one. A single job may list several sources that are bundled into **one atomic snapshot**.

```json
{
  "name": "main",
  "sources": [ { "type": "...", ... } ],
  "destinations": [ {"type": "local"}, {"type": "s3", ...} ],
  "keep_local": true,
  "schedule": { ... },
  "retention": { ... },
  "encryption": { ... },
  "notifications": { ... }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `name` | `"main"` | Job name (used in schedule ids, alerts, restore `--job`) |
| `sources` | `[]` | What to back up — see [sources](sources.md) |
| `destinations` | `[{"type":"local"}]` | Where to store it — `local` and/or `s3`, see [destinations](destinations.md) |
| `keep_local` | `true` | When `false`, delete the local copy after a successful off-site S3 upload |
| `schedule` | see below | When to run |
| `retention` | see below | How many/long to keep — see [retention](retention.md) |
| `encryption` | `{"mode":"none"}` | Optional age/gpg — see [encryption](encryption.md) |
| `notifications` | `{"channels":[]}` | Alerts — see [notifications](notifications.md) |

### Schedule

| Field | Default | Description |
| --- | --- | --- |
| `mode` | `"cron"` | `cron` or `interval` |
| `cron` | `"15 3 * * *"` | 5-field cron string (cron mode) |
| `interval_hours` | `24` | Fixed interval in hours (interval mode) |
| `on_startup` | `false` | Also run once immediately on container start |
| `hour` / `minute` / `day_of_week` | `null` | Field-based alternative to a raw cron string |

### Retention

| Field | Default | Description |
| --- | --- | --- |
| `count` | `14` | Keep the newest N; `<= 0` keeps everything |
| `age_days` | `0` | Also prune older than N days; `0` disables |
| `gfs.daily` / `gfs.weekly` / `gfs.monthly` | `0` | Grandfather-father-son keep-counts per tier |
| `smart_last` | `true` | Never prune the sole/last backup |

## Run modes

The same config drives all modes:

```bash
backuphelper                 # scheduler daemon (default)
backuphelper --now           # run every job once and exit
backuphelper <command> ...   # CLI — see docs/cli.md
```

See the [CLI reference](cli.md) for every command.
