#!/usr/bin/env bash
# =============================================================================
# BackupHelper — end-to-end test matrix
# -----------------------------------------------------------------------------
# Runs a real backup -> (local + MinIO S3) -> restore roundtrip against every
# source engine using docker-compose.e2e.yml. Each engine is seeded, backed up,
# has its data destroyed, restored, and asserted.
#
#   ./scripts/e2e.sh            # run the matrix, tear down at the end
#   ./scripts/e2e.sh --keep     # leave the stack running for inspection
# =============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1   # keep /paths literal for git-bash on Windows

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.e2e.yml"
KEEP="${1:-}"
PASS=0; FAIL=0
ok(){ echo "  [PASS] $1"; PASS=$((PASS+1)); }
ko(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

cleanup(){ [ "$KEEP" = "--keep" ] || { echo "== teardown =="; $COMPOSE down -v >/dev/null 2>&1; }; }
trap cleanup EXIT

backup_now(){ $COMPOSE run --rm -e BACKUP_CONFIG_JSON="$1" backup --now 2>&1; }
sid_of(){ grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}' | head -1; }
do_restore(){ $COMPOSE run --rm -e BACKUP_CONFIG_JSON="$1" backup restore "$2" --only "$3" --force 2>&1; }
mc(){ docker run --rm --network bh-e2e --entrypoint sh minio/mc:latest -c \
        "mc alias set m http://minio:9000 admin minioadmin-dev >/dev/null 2>&1 && $1" 2>&1; }
in_files(){ $COMPOSE run --rm --entrypoint sh backup -c "$1" 2>&1; }

dest='{"type":"s3","endpoint":"http://minio:9000","bucket":"backups","access_key":"backup-app","secret_key":"backup-secret-dev","region":"eu-central-1","force_path_style":true,"ensure_bucket":false,"prefix":"PFX/"}'

# ── bring up infra ───────────────────────────────────────────────────────────
echo "== build backup image =="
$COMPOSE build backup >/dev/null 2>&1 || { echo "build failed"; exit 1; }
echo "== start infra =="
$COMPOSE up -d postgres mariadb mysql minio minio-init >/dev/null 2>&1

echo "== wait for databases + minio-init =="
for i in $(seq 1 30); do
  ph=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_POSTGRES 2>/dev/null)
  mh=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_MARIADB 2>/dev/null)
  yh=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_MYSQL 2>/dev/null)
  ii=$(docker inspect -f '{{.State.Status}}:{{.State.ExitCode}}' bh-e2e_MINIO_INIT 2>/dev/null)
  echo "  postgres=$ph mariadb=$mh mysql=$yh minio-init=$ii"
  [ "$ph" = healthy ] && [ "$mh" = healthy ] && [ "$yh" = healthy ] && [ "$ii" = "exited:0" ] && break
  sleep 5
done

# ── PostgreSQL ───────────────────────────────────────────────────────────────
echo "== engine: postgres =="
$COMPOSE exec -T postgres psql -U app -d app -c \
  "DROP TABLE IF EXISTS demo; CREATE TABLE demo(id int PRIMARY KEY, name text); INSERT INTO demo VALUES (1,'e2e-original');" >/dev/null 2>&1
pg='{"instance_name":"e2e","jobs":[{"name":"pg","sources":[{"type":"postgres","host":"postgres","database":"app","user":"app","password":"devpassword"}],"destinations":[{"type":"local"},'"${dest/PFX/pg}"']}]}'
out=$(backup_now "$pg"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "postgres backup ($sid)" || ko "postgres backup ($sid)"
mc "mc ls m/backups/pg/" | grep -q "$sid" && ok "postgres archive in MinIO" || ko "postgres archive in MinIO"
$COMPOSE exec -T postgres psql -U app -d app -c "DROP TABLE demo;" >/dev/null 2>&1
do_restore "$pg" "$sid" app >/dev/null 2>&1
$COMPOSE exec -T postgres psql -U app -d app -tAc "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original" \
  && ok "postgres restore roundtrip" || ko "postgres restore roundtrip"

# ── MariaDB ──────────────────────────────────────────────────────────────────
echo "== engine: mariadb =="
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e \
  "DROP TABLE IF EXISTS demo; CREATE TABLE demo(id int PRIMARY KEY, name varchar(64)); INSERT INTO demo VALUES (1,'e2e-original');" 2>/dev/null
maria='{"instance_name":"e2e","jobs":[{"name":"maria","sources":[{"type":"mariadb","host":"mariadb","database":"app","user":"app","password":"devpassword"}],"destinations":[{"type":"local"},'"${dest/PFX/maria}"']}]}'
out=$(backup_now "$maria"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "mariadb backup ($sid)" || ko "mariadb backup ($sid)"
mc "mc ls m/backups/maria/" | grep -q "$sid" && ok "mariadb archive in MinIO" || ko "mariadb archive in MinIO"
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e "DROP TABLE demo;" 2>/dev/null
do_restore "$maria" "$sid" app >/dev/null 2>&1
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -N -e "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original" \
  && ok "mariadb restore roundtrip" || ko "mariadb restore roundtrip"

# ── MySQL ────────────────────────────────────────────────────────────────────
echo "== engine: mysql =="
$COMPOSE exec -T mysql mysql -uroot -prootpw app -e \
  "DROP TABLE IF EXISTS demo; CREATE TABLE demo(id int PRIMARY KEY, name varchar(64)); INSERT INTO demo VALUES (1,'e2e-original');" 2>/dev/null
mysql='{"instance_name":"e2e","jobs":[{"name":"mysql","sources":[{"type":"mysql","host":"mysql","database":"app","user":"root","password":"rootpw"}],"destinations":[{"type":"local"},'"${dest/PFX/mysql}"']}]}'
out=$(backup_now "$mysql"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "mysql backup ($sid)" || ko "mysql backup ($sid)"
mc "mc ls m/backups/mysql/" | grep -q "$sid" && ok "mysql archive in MinIO" || ko "mysql archive in MinIO"
$COMPOSE exec -T mysql mysql -uroot -prootpw app -e "DROP TABLE demo;" 2>/dev/null
do_restore "$mysql" "$sid" app >/dev/null 2>&1
$COMPOSE exec -T mysql mysql -uroot -prootpw app -N -e "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original" \
  && ok "mysql restore roundtrip" || ko "mysql restore roundtrip"

# ── Filesystem (local files) ─────────────────────────────────────────────────
echo "== engine: filesystem =="
# Seed as root and hand /files to the non-root backup uid so it can read (for
# backup) and write (for restore). In production the restore target volume must
# likewise be writable by the container's uid.
$COMPOSE run --rm --user 0 --entrypoint sh backup -c \
  "rm -rf /files/* && mkdir -p /files/sub && echo hello-fs > /files/note.txt && echo nested > /files/sub/b.txt && chown -R 1000:1000 /files" >/dev/null 2>&1
fs='{"instance_name":"e2e","jobs":[{"name":"fs","sources":[{"type":"filesystem","name":"data","path":"/files"}],"destinations":[{"type":"local"},'"${dest/PFX/files}"']}]}'
out=$(backup_now "$fs"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "filesystem backup ($sid)" || ko "filesystem backup ($sid)"
mc "mc ls m/backups/files/" | grep -q "$sid" && ok "filesystem archive in MinIO" || ko "filesystem archive in MinIO"
in_files "rm -rf /files/note.txt /files/sub" >/dev/null 2>&1
do_restore "$fs" "$sid" data >/dev/null 2>&1
out=$(in_files "cat /files/note.txt; cat /files/sub/b.txt")
echo "$out" | grep -q "hello-fs" && echo "$out" | grep -q "nested" \
  && ok "filesystem restore roundtrip" || ko "filesystem restore roundtrip"

# ── S3-bucket source (mirror with per-object metadata) ───────────────────────
echo "== engine: s3-bucket-source =="
mc "printf 'image-bytes' > /tmp/o.bin && mc put --quiet /tmp/o.bin m/assets/photos/cat.bin && mc tag set m/assets/photos/cat.bin 'env=prod'" >/dev/null 2>&1
s3='{"instance_name":"e2e","jobs":[{"name":"s3","sources":[{"type":"s3","name":"assets","endpoint":"http://minio:9000","bucket":"assets","access_key":"backup-app","secret_key":"backup-secret-dev","region":"eu-central-1","force_path_style":true}],"destinations":[{"type":"local"},'"${dest/PFX/s3mirror}"']}]}'
out=$(backup_now "$s3"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "s3-source backup ($sid)" || ko "s3-source backup ($sid)"
mc "mc ls m/backups/s3mirror/" | grep -q "$sid" && ok "s3-source archive in MinIO" || ko "s3-source archive in MinIO"
mc "mc rm m/assets/photos/cat.bin" >/dev/null 2>&1
do_restore "$s3" "$sid" assets >/dev/null 2>&1
mc "mc cat m/assets/photos/cat.bin" | grep -q "image-bytes" && ok "s3-source restore (object back)" || ko "s3-source restore (object back)"
mc "mc tag list m/assets/photos/cat.bin" | grep -q "env" && ok "s3-source restore (tags preserved)" || ko "s3-source restore (tags preserved)"

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
echo "== E2E result: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
