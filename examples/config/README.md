Ready-to-adapt `BACKUP_CONFIG_JSON` / `BACKUP_CONFIG_FILE` examples, one per common use case. Copy one, replace the `${VAR}` secret references with your own env vars, and pass it via `BACKUP_CONFIG_JSON`, `BACKUP_CONFIG_FILE` or base64. See [../../docs/configuration.md](../../docs/configuration.md).

| File | Use case |
| --- | --- |
| [postgres-local.json](postgres-local.json) | PostgreSQL → local only |
| [postgres-s3.json](postgres-s3.json) | PostgreSQL → local + off-site S3, age-based retention |
| [mariadb-files.json](mariadb-files.json) | MariaDB + two filesystem path-groups (uploads / plugins-themes-languages) → S3 |
| [mysql.json](mysql.json) | MySQL multi-database, interval schedule |
| [s3-bucket-mirror.json](s3-bucket-mirror.json) | Mirror an S3 bucket (with per-object metadata) to another provider |
| [multi-source-bundle.json](multi-source-bundle.json) | DB + S3 bucket + env snapshot in one atomic bundle |
| [multi-job.json](multi-job.json) | Two independent jobs (hourly DB, nightly files) in one container |
| [encrypted.json](encrypted.json) | Client-side age encryption before off-site upload |
| [gfs-retention.json](gfs-retention.json) | Grandfather-father-son retention (7 daily / 4 weekly / 12 monthly) |
| [all-notifications.json](all-notifications.json) | Every notification channel wired up |

### Using an example

```bash
# inline
export BACKUP_CONFIG_JSON="$(cat postgres-s3.json)"

# base64 (avoids compose quoting issues)
export BACKUP_CONFIG_JSON_BASE64="$(base64 -w0 postgres-s3.json)"

# mounted file
docker run -v "$PWD/postgres-s3.json:/config/backup.json:ro" \
  -e BACKUP_CONFIG_FILE=/config/backup.json \
  ghcr.io/bauer-group/cs-backuphelper/backuphelper:latest --now
```
