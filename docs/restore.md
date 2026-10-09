How to restore a BackupHelper snapshot end to end — pick it, verify it, then replay it onto the live sources. Restore is **destructive**: read this before running it against anything you cannot lose.

## How restore works

`restore` reverses the backup pipeline for one snapshot:

1. **Locate** the artifact (`<id>.tar.gz`, optionally `.age`/`.gpg`) and its sidecar manifest (`<id>.manifest.json`) in the data dir. Both must be present.
2. **Auto-decrypt** the artifact if it ends in `.age` or `.gpg` (see [Encryption](#encryption)).
3. **Extract** the outer bundle into a temporary work dir.
4. **Replay each component** listed in the manifest onto its matching configured source. Components that errored during backup, or that you excluded with `--only`, are skipped. A component with no matching source config in the selected job is logged and skipped.

Before it decrypts anything, restore recomputes the archive's sha256 and refuses (exit `1`) when it does not match `archive_sha256` in the sidecar manifest; a manifest without that field is not checked. `verify` runs the same check without touching anything, so run it first (step 2 below) to find a bad snapshot before you stop the application.

## Step 1 — pick the snapshot

List what is available and choose an id:

```bash
docker compose run --rm backup list
# 2026-07-05_03-15-00         48210433 bytes
# 2026-07-04_03-15-00         48117902 bytes
```

Inspect a snapshot's manifest to see exactly which components it holds before you touch live data:

```bash
docker compose run --rm backup show 2026-07-05_03-15-00
```

If the snapshot lives off-box (was exported with `download` or pulled from S3), copy **both** the archive and its `.manifest.json` back into the data dir first — restore needs the sidecar manifest, not just the archive.

## Step 2 — verify integrity (the gate)

Always verify before restoring. `verify` recomputes the archive sha256 and compares it to `archive_sha256` in the sidecar manifest:

```bash
docker compose run --rm backup verify 2026-07-05_03-15-00
# OK 2026-07-05_03-15-00
```

`OK` exits `0`; a mismatch (or a missing archive/manifest hash) prints `FAILED` and exits `2`. Do not restore a snapshot that fails verification — the archive is corrupt or truncated.

## Step 3 — restore (destructive)

Once verified, restore. Without `--force` the command prompts before overwriting; in a non-interactive container run you must pass `--force`:

```bash
docker compose run --rm backup restore 2026-07-05_03-15-00 --force
# restore complete
```

This **overwrites live data** for the selected job's sources. Databases are dropped-and-reloaded, S3 objects are re-uploaded, filesystem trees are overlaid. There is no undo.

## Selecting what to restore

| Flag | Effect |
| ---- | ------ |
| `--job <name>` | Choose which configured job's sources receive the restore. Defaults to the **first** job. Fails with exit `1` if the name matches no job. |
| `--only <component>` | Restore only the named component(s). Repeatable (`--only database --only uploads`). Names are the manifest component names shown by `show`. Everything not listed is skipped. The selection is validated before anything is touched (no hook runs, no data is written): a name that is not in the snapshot, that failed at backup time, or that has no matching source in the selected job fails the restore with exit `1`, and the log lists the valid component names. |

```bash
# Only bring back the 'uploads' filesystem tree, leave the DB untouched
docker compose run --rm backup \
  restore 2026-07-05_03-15-00 --job main --only uploads --force
```

## Per-source restore behaviour

Each component is replayed by its own source type. The behaviours differ in how destructive and how complete they are:

| Component kind | Restore action | Destructive? |
| -------------- | -------------- | ------------ |
| `postgres` | `pg_restore --clean --if-exists --no-owner --no-acl --single-transaction` (privileges re-applied instead with `keep_acl`) into the target DB for custom-format `.dump`; gunzipped `.sql.gz` plain dumps are streamed through `psql`. `--clean --if-exists` drops existing objects before recreating them; partitioned tables of the target that the dump recreates are dropped first, in the same transaction (see [sources](sources.md#postgres)). | Yes — full DB replace |
| `mariadb` / `mysql` | Gunzipped `.sql.gz` logical dump streamed into the `mariadb`/`mysql` client. The dump's own `DROP`/`CREATE` statements replay over the live database. | Yes — full DB replace |
| `filesystem` | The extracted tree is **overlaid** onto the configured `path` with `copy2` — files are created/overwritten. Note this is an overlay, **not** a mirror: files present in the live target but absent from the backup are **not** deleted. | Partial — overwrites, never deletes |
| `s3` | Every captured object is re-`PUT` to the bucket **with its original metadata** — the content headers (`Content-Type`, `Content-Disposition`, `Cache-Control`, `Content-Encoding`, `Content-Language`), user metadata and tags are re-applied from the captured `metadata.json`. | Yes — objects overwritten by key |
| `env` | **Not applied.** Env snapshots are informational only; restore treats them as a no-op. To re-apply environment variables, set them yourself (or wire a repo lifecycle hook). | No |

Restore uses the same source configuration as backup, so the target host/credentials come from the selected job's source specs (see [sources](sources.md)). Component-to-source matching is by name: a source's component name (its explicit `name`, or the database name for DB sources) must equal the manifest component name.

## Encryption

If the artifact is encrypted, restore decrypts it automatically based on the file suffix:

- `.age` → decrypted with `age`
- `.gpg` → decrypted with `gpg`

The matching key material must be available to the container (the same identity/recipient used to encrypt). Configure this exactly as for backup — see [configuration](configuration.md). Plain `.tar.gz` artifacts skip this step.

## Disaster-recovery walkthrough

A worked example: the application's Postgres database and its `/uploads` tree are lost, and you need to bring the most recent good snapshot back.

```bash
# 1. Confirm the DB clients and config are what you expect
docker compose run --rm backup config

# 2. Find the newest snapshot
docker compose run --rm backup list
#    2026-07-05_03-15-00   48210433 bytes   <- newest

# 3. Inspect its components (expect: database, uploads)
docker compose run --rm backup show 2026-07-05_03-15-00

# 4. GATE: verify the archive against its manifest sha256
docker compose run --rm backup verify 2026-07-05_03-15-00
#    OK 2026-07-05_03-15-00

# 5. (Optional) stop the app so nothing writes mid-restore
docker compose stop app

# 6. Restore everything for the job (destructive), no prompt
docker compose run --rm backup restore 2026-07-05_03-15-00 --force
#    restore complete

# 7. Restart the app and validate
docker compose start app
```

If only one component was damaged, scope the restore with `--only` (e.g. `--only database`) so you don't needlessly overwrite the healthy filesystem tree.

If the snapshot is only available off-site, first copy the archive **and** its `.manifest.json` into the data volume, then start at step 4.

## Limitations and caveats

- **Validate DB restore against staging first.** The database restore paths (Postgres `pg_restore`/`psql`, MariaDB/MySQL client replay) are covered by unit tests and by real-server round trips in CI (`scripts/e2e.sh`: PostgreSQL 18 incl. partitioned tables and grants; MariaDB and MySQL in their LTS lines 11.4 and 8.4, their newest releases and the versions in `docker-compose.e2e.yml`), but have not been proven against a production-scale live database. Before relying on them for a real recovery, rehearse the full restore against a **staging** copy of the target DB and confirm the data and schema come back intact.
- **Filesystem restore is additive.** It overwrites and adds files but never deletes stray files already on disk. For a byte-exact tree, restore into an empty/clean target path.
- **`env` is never auto-applied.** Environment variables are captured for reference only; you must re-apply them yourself.
- **Restore refuses a corrupt archive.** It re-checks `archive_sha256` before decrypting and stops on a mismatch. Still run `verify` first, so a bad snapshot shows up before the application is stopped.
- **No undo.** Databases and S3 objects are overwritten in place. Take a fresh backup (or a manual DB dump) of the current state before restoring if there is any chance the current data is still worth keeping.
