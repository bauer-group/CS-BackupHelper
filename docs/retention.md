Retention decides which snapshots to keep and which to prune. Four independent policies — count, age, GFS and smart-last — are composed into a single prune decision and applied per destination after every run.

## The snapshot model

Every policy reasons over `Snapshot` objects, each with:

- **`id`** — a sortable timestamp string (`%Y-%m-%d_%H-%M-%S`). Newest = lexicographically greatest.
- **`when`** — the datetime the snapshot was taken.

The policies are pure functions: they select ids to prune or keep and perform no I/O. The runner and the [`prune` CLI](#the-prune-cli) then act on that selection.

## Configuration

```json
{
  "retention": {
    "count": 14,
    "age_days": 90,
    "gfs": { "daily": 7, "weekly": 4, "monthly": 6 },
    "smart_last": true
  }
}
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `count` | int | `14` | Keep the newest `count` snapshots; prune the rest. **`count <= 0` keeps everything** (a safety rule — prune nothing). |
| `age_days` | int | `0` | Prune snapshots older than `now - age_days`. **`0` disables** age-based pruning. |
| `gfs.daily` | int | `0` | Keep the newest snapshot of the newest N calendar days. `0` disables the tier. |
| `gfs.weekly` | int | `0` | Keep the newest snapshot of the newest N ISO weeks. `0` disables the tier. |
| `gfs.monthly` | int | `0` | Keep the newest snapshot of the newest N year-months. `0` disables the tier. |
| `smart_last` | bool | `true` | Never prune the single newest snapshot, even if the policies above would. |

See the [configuration](configuration.md) reference for where `retention` sits inside a job.

## The four policies

### Count

Keeps the newest `count` snapshots by id, prunes the rest.

- `count = 14` → keep the 14 newest snapshots, prune everything older.
- `count <= 0` → **keep everything** (prune nothing). This is a deliberate safety default: a misconfigured or zeroed count never wipes your history.

### Age

Prunes any snapshot whose `when` is older than the cutoff `now - age_days`.

- `age_days = 90` → prune snapshots older than 90 days.
- `age_days = 0` → age-based pruning is disabled (prune nothing on this axis).

### GFS (grandfather-father-son)

A *keep* policy across three tiers. Each tier keeps the newest snapshot of the newest N distinct buckets:

| Tier | Bucket | Example config | Keeps |
| --- | --- | --- | --- |
| `daily` | calendar day `(year, month, day)` | `7` | one snapshot per day for the 7 most recent days that have a snapshot |
| `weekly` | ISO week `(iso-year, iso-week)` | `4` | one snapshot per week for the 4 most recent weeks |
| `monthly` | year-month `(year, month)` | `6` | one snapshot per month for the 6 most recent months |

Within a bucket the **newest** snapshot (greatest id) is the one kept. A tier set to `0` is disabled. The kept sets **union** across tiers, so a single snapshot can satisfy more than one tier.

### Smart-last

Protects the single newest snapshot from pruning. When `smart_last` is `true`, the most recent snapshot is always retained — so retention can never leave a source with zero backups. Enabled by default.

## How the policies compose

The retention manager combines them into one prune set:

```
prune = (count_prunable ∪ age_prunable) − gfs_keep − smart_protected
```

In words: a snapshot is pruned only if **count or age** selects it, **and** it is **not** protected by any GFS tier, **and** it is **not** the smart-last snapshot. GFS keeps and smart-last protection are safety overrides — they always win over the count/age selectors.

`smart_protected` is empty when `smart_last` is `false`.

## Worked example

Config: `count = 14`, `gfs = { daily: 7, weekly: 4, monthly: 6 }`, `smart_last: true`, `age_days: 0`, with daily snapshots taken over several months.

1. **count** marks everything older than the 14 newest for pruning.
2. **GFS** rescues a spread of older snapshots from that set: the newest of each of the last 7 days, 4 ISO weeks and 6 months (unioned) — so you retain roughly six months of history at decreasing granularity instead of only the last 14 days.
3. **smart-last** guarantees the newest snapshot survives no matter what.

The net effect: dense recent coverage (14 latest + last 7 days) tapering to weekly and then monthly checkpoints, plus a hard guarantee that at least the latest backup is always kept.

**`count <= 0` safety example:** with `count: 0` and every GFS tier `0` and `age_days: 0`, no policy selects anything to prune — the entire history is kept. This is intentional: zeroed retention never deletes.

## Retention applies per destination

Retention runs **independently for each configured destination**. After uploading a snapshot, the runner lists the snapshots that actually exist on each destination (local, S3) and applies the policy to that destination's own set. A destination that already holds a different set of snapshots (e.g. an off-site S3 target that has been offline) is pruned against its own contents, not the local view.

## The `prune` CLI

Retention also runs automatically after every scheduled backup. To apply it on demand to the **local** data directory:

```bash
backuphelper prune              # apply the job's retention policy to local snapshots
backuphelper prune --dry-run    # print what would be pruned, delete nothing
backuphelper prune --keep 30    # override count to 30 for this run
```

| Flag | Meaning |
| --- | --- |
| `--keep N` | Override the `count` field with `N` for this invocation (other policies unchanged). |
| `--dry-run` | List the snapshots that would be pruned without deleting anything. |

The command uses the first job's `retention` config, evaluates it over the local snapshots (found by their `*.manifest.json` sidecars), and — unless `--dry-run` — deletes every file belonging to each pruned snapshot id.

The `prune` CLI parses the real timestamp from each snapshot id (the same logic the scheduled runner uses), so all four policies — count, age, GFS and smart-last — behave identically whether pruning runs automatically after a backup or manually via the CLI.
