# shellcheck shell=bash
# =============================================================================
# e2e suite "engines" — sourced by scripts/e2e.sh (helpers, $COMPOSE, ok/ko)
# -----------------------------------------------------------------------------
# A real backup -> (local + MinIO S3) -> restore round trip against every source
# engine of docker-compose.e2e.yml: each one is seeded, backed up, has its data
# destroyed, restored and asserted. The backups and restores run in the engine
# image itself, so they use its own clients (pg_dump/pg_restore,
# mariadb-dump/mariadb).
# =============================================================================

# archived <prefix> <snapshot id> <label>: the snapshot reached the MinIO bucket.
archived(){
  local listing; listing=$(mc "mc ls m/backups/$1/")
  printf '%s' "$listing" | grep -q "$2" && ok "$3 archive in MinIO" || ko "$3 archive in MinIO" "$listing"
}
# mc from the CS-MinIO init image (the minio/mc image is no longer published).
mc(){ docker run --rm --network bh-e2e --entrypoint sh ghcr.io/bauer-group/cs-minio/minio-init:latest -c \
        "mc alias set m http://minio:9000 admin minioadmin-dev >/dev/null 2>&1 && $1" 2>&1; }
# SQL from stdin, run in the server containers with the root password from
# their own environment (no credentials in this script).
psql_app(){ $COMPOSE exec -T postgres psql -U app -d app -v ON_ERROR_STOP=1 -X -q -tA "$@" 2>&1; }
maria_sql(){ $COMPOSE exec -T mariadb sh -c \
        'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" --default-character-set=utf8mb4 -N app' 2>&1; }
maria_app_sql(){ $COMPOSE exec -T mariadb sh -c \
        'exec mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" --default-character-set=utf8mb4 -N app' 2>&1; }
mysql_sql(){ $COMPOSE exec -T mysql sh -c \
        'exec mysql -uroot --password="$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 -N app' 2>&1 \
        | grep -v 'Using a password on the command line'; }

dest='{"type":"s3","endpoint":"http://minio:9000","bucket":"backups","access_key":"backup-app","secret_key":"backup-secret-dev","region":"eu-central-1","force_path_style":true,"ensure_bucket":false,"prefix":"PFX/"}'

# ── bring up infra ───────────────────────────────────────────────────────────
echo "== start infra =="
$COMPOSE up -d postgres mariadb mysql minio minio-init >/dev/null 2>&1

echo "== wait for databases + minio-init =="
for _ in $(seq 1 30); do
  ph=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_POSTGRES 2>/dev/null)
  mh=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_MARIADB 2>/dev/null)
  yh=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_MYSQL 2>/dev/null)
  ii=$(docker inspect -f '{{.State.Status}}:{{.State.ExitCode}}' bh-e2e_MINIO_INIT 2>/dev/null)
  echo "  postgres=$ph mariadb=$mh mysql=$yh minio-init=$ii"
  [ "$ph" = healthy ] && [ "$mh" = healthy ] && [ "$yh" = healthy ] && [ "$ii" = "exited:0" ] && break
  sleep 5
done

echo "== versions under test =="
echo "  postgres server: $(psql_app -c 'SHOW server_version' | head -1)"
echo "  mariadb server:  $(echo 'SELECT VERSION();' | maria_sql | head -1)"
echo "  mysql server:    $(echo 'SELECT VERSION();' | mysql_sql | head -1)"
in_files 'echo "  engine clients:  $(pg_dump --version) / $(mariadb-dump --version)";
          echo "  client plugins:  $(ls /usr/lib/mariadb/plugin 2>/dev/null | tr "\n" " ")"'

# ── PostgreSQL ───────────────────────────────────────────────────────────────
echo "== engine: postgres =="
# demo: a plain table. events: a partitioned table, as Zitadel's cache tables or
# a pg_partman schema — pg_restore --clean alone cannot restore over one that
# exists ("cannot drop inherited constraint"). app_runtime: a restricted role
# the application would connect as, with grants on both.
psql_app >/dev/null <<'SQL'
DROP TABLE IF EXISTS demo, events CASCADE;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_runtime') THEN
    CREATE ROLE app_runtime NOLOGIN;
  END IF;
END $$;
CREATE TABLE demo(id int PRIMARY KEY, name text);
INSERT INTO demo VALUES (1, 'e2e-original');
CREATE TABLE events(id int NOT NULL, at date NOT NULL, note text, PRIMARY KEY (id, at))
  PARTITION BY RANGE (at);
CREATE TABLE events_2025 PARTITION OF events FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE events_2026 PARTITION OF events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
INSERT INTO events VALUES (1, '2025-06-01', 'old'), (2, '2026-06-01', 'new');
GRANT USAGE ON SCHEMA public TO app_runtime;
GRANT SELECT, INSERT ON demo, events TO app_runtime;
SQL
# "<demo name>|<event ids>|<partitions>|<runtime SELECT on demo>|<runtime INSERT on events>"
pg_state(){ psql_app <<'SQL'
SELECT concat_ws('|', (SELECT name FROM demo WHERE id = 1),
                 (SELECT string_agg(id::text, ',' ORDER BY id) FROM events),
                 (SELECT count(*) FROM pg_inherits WHERE inhparent = 'public.events'::regclass),
                 has_table_privilege('app_runtime', 'public.demo', 'SELECT'),
                 has_table_privilege('app_runtime', 'public.events', 'INSERT'));
SQL
}
pg='{"instance_name":"e2e","jobs":[{"name":"pg","sources":[{"type":"postgres","host":"postgres","database":"app","user":"app","password":"devpassword"}],"destinations":[{"type":"local"},'"${dest/PFX/pg}"']}]}'
out=$(backup_now "$pg"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "postgres backup ($sid)" || ko "postgres backup ($sid)" "$out"
archived pg "$sid" postgres
# (a) the tables are gone (a fresh database): the plain pg_restore path
psql_app -c "DROP TABLE demo, events;" >/dev/null
out=$(do_restore "$pg" "$sid" app); st=$(pg_state)
[ "$st" = "e2e-original|1,2|2|f|f" ] \
  && ok "postgres restore into an empty database (grants dropped by default)" \
  || ko "postgres restore into an empty database: got '$st'" "$out"
# (b) in place, over the live partitioned table: changed rows come back
psql_app -c "UPDATE demo SET name = 'mutated' WHERE id = 1; DELETE FROM events WHERE id = 1;
             INSERT INTO events VALUES (3, '2026-07-01', 'after-backup');" >/dev/null
out=$(do_restore "$pg" "$sid" app); st=$(pg_state)
[ "$st" = "e2e-original|1,2|2|f|f" ] \
  && ok "postgres restore over a live partitioned table" \
  || ko "postgres restore over a live partitioned table: got '$st'" "$out"
# (c) keep_acl: the runtime role keeps its grants across the restore
pg_acl=$(printf '%s' "$pg" | sed 's/"name":"pg"/"name":"pgacl"/; s/"password":/"keep_acl":true,"password":/')
psql_app -c "GRANT SELECT, INSERT ON demo, events TO app_runtime;" >/dev/null
out=$(backup_now "$pg_acl"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "postgres backup with keep_acl ($sid)" || ko "postgres backup with keep_acl ($sid)" "$out"
show_snapshot "$sid" | grep -q '"acl": true' && ok "postgres keep_acl marked in the manifest" || ko "postgres keep_acl marked in the manifest"
psql_app -c "REVOKE ALL ON demo, events FROM app_runtime; UPDATE demo SET name = 'mutated' WHERE id = 1;" >/dev/null
out=$(do_restore "$pg_acl" "$sid" app); st=$(pg_state)
[ "$st" = "e2e-original|1,2|2|t|t" ] \
  && ok "postgres restore with keep_acl keeps the runtime role's grants" \
  || ko "postgres restore with keep_acl: got '$st'" "$out"

# ── MariaDB ──────────────────────────────────────────────────────────────────
echo "== engine: mariadb =="
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e \
  "DROP TABLE IF EXISTS demo; CREATE TABLE demo(id int PRIMARY KEY, name varchar(64)); INSERT INTO demo VALUES (1,'e2e-original');" 2>/dev/null
# 4-byte UTF-8 (--default-character-set=utf8mb4), a trigger (--triggers) and a
# stored procedure (--routines), created by the application user like an
# application would: the dump keeps their DEFINER.
extras_seed="ALTER TABLE demo CONVERT TO CHARACTER SET utf8mb4;
INSERT INTO demo VALUES (2, 'utf8mb4 😀 ü');
CREATE TRIGGER demo_bi BEFORE INSERT ON demo FOR EACH ROW SET NEW.name = TRIM(NEW.name);
DROP PROCEDURE IF EXISTS demo_count;
CREATE PROCEDURE demo_count(OUT n INT) SELECT COUNT(*) INTO n FROM demo;"
echo "$extras_seed" | maria_app_sql
# "<4-byte text intact>|<trigger present>|<procedure present>"
extras_sql="SELECT CONCAT((SELECT name FROM demo WHERE id = 2) = 'utf8mb4 😀 ü', '|',
  (SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'app' AND TRIGGER_NAME = 'demo_bi'), '|',
  (SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'app' AND ROUTINE_NAME = 'demo_count'));"
maria='{"instance_name":"e2e","jobs":[{"name":"maria","sources":[{"type":"mariadb","host":"mariadb","database":"app","user":"app","password":"devpassword"}],"destinations":[{"type":"local"},'"${dest/PFX/maria}"']}]}'
out=$(backup_now "$maria"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "mariadb backup ($sid)" || ko "mariadb backup ($sid)" "$out"
archived maria "$sid" mariadb
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e "DROP TABLE demo;" 2>/dev/null
echo "DROP PROCEDURE demo_count;" | maria_sql >/dev/null
out=$(do_restore "$maria" "$sid" app)
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -N -e "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original"
restored "mariadb restore roundtrip" "$out" $?
st=$(echo "$extras_sql" | maria_sql)
[ "$st" = "1|1|1" ] && ok "mariadb restore keeps utf8mb4 text, trigger and procedure" \
  || ko "mariadb utf8mb4 text, trigger and procedure: got '$st'"

# ── MySQL ────────────────────────────────────────────────────────────────────
echo "== engine: mysql =="
$COMPOSE exec -T mysql mysql -uroot -prootpw app -e \
  "DROP TABLE IF EXISTS demo; CREATE TABLE demo(id int PRIMARY KEY, name varchar(64)); INSERT INTO demo VALUES (1,'e2e-original');" 2>/dev/null
echo "$extras_seed" | mysql_sql
# The engine logs in as its own user with the server's default authentication
# (caching_sha2_password). The healthcheck keeps root's password in the
# server's auth cache, which would let a root login skip the full
# authentication; a fresh user and FLUSH PRIVILEGES before each run make every
# backup and restore take the full exchange, as after a server restart.
bk_secret=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
mysql_sql >/dev/null <<SQL
CREATE USER 'bk'@'%' IDENTIFIED BY '$bk_secret';
GRANT ALL PRIVILEGES ON *.* TO 'bk'@'%';
SQL
echo "  backup user auth: $(echo "SELECT plugin FROM mysql.user WHERE user = 'bk';" | mysql_sql)"
mysql='{"instance_name":"e2e","jobs":[{"name":"mysql","sources":[{"type":"mysql","host":"mysql","database":"app","user":"root","password":"rootpw"}],"destinations":[{"type":"local"},'"${dest/PFX/mysql}"']}]}'
mysql=$(printf '%s' "$mysql" | sed -E 's/"user":"root","password":"[^"]*"/"user":"bk","password":"'"$bk_secret"'"/')
echo "FLUSH PRIVILEGES;" | mysql_sql >/dev/null
out=$(backup_now "$mysql"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "mysql backup ($sid)" || ko "mysql backup ($sid)" "$out"
archived mysql "$sid" mysql
$COMPOSE exec -T mysql mysql -uroot -prootpw app -e "DROP TABLE demo;" 2>/dev/null
echo "DROP PROCEDURE demo_count; FLUSH PRIVILEGES;" | mysql_sql >/dev/null
out=$(do_restore "$mysql" "$sid" app)
$COMPOSE exec -T mysql mysql -uroot -prootpw app -N -e "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original"
restored "mysql restore roundtrip" "$out" $?
st=$(echo "$extras_sql" | mysql_sql)
[ "$st" = "1|1|1" ] && ok "mysql restore keeps utf8mb4 text, trigger and procedure" \
  || ko "mysql utf8mb4 text, trigger and procedure: got '$st'"

# Least-privilege backup users (on MySQL 26+ the routines come from a second
# pass). bk_min holds what the dump needs - SELECT, SHOW VIEW, TRIGGER, EVENT on
# the database, SHOW_ROUTINE to read routine bodies - but no LOCK TABLES: no
# pass may lock tables. bk_exec sees the procedure only through EXECUTE, so
# MySQL hides its body: that backup must fail, not succeed without it.
min_secret=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
exec_secret=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
mysql_sql >/dev/null <<SQL
CREATE USER 'bk_min'@'%' IDENTIFIED BY '$min_secret';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON app.* TO 'bk_min'@'%';
GRANT SHOW_ROUTINE ON *.* TO 'bk_min'@'%';
CREATE USER 'bk_exec'@'%' IDENTIFIED BY '$exec_secret';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, EXECUTE ON app.* TO 'bk_exec'@'%';
FLUSH PRIVILEGES;
SQL
# as_user <job> <user> <password>: the mysql job under another name and login.
as_user(){ printf '%s' "$mysql" | sed -E 's/"name":"mysql"/"name":"'"$1"'"/;
             s/"user":"bk","password":"[^"]*"/"user":"'"$2"'","password":"'"$3"'"/'; }
out=$(backup_now "$(as_user mysqlmin bk_min "$min_secret")"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" \
  && ok "mysql backup as a user without LOCK TABLES ($sid)" \
  || ko "mysql backup as a user without LOCK TABLES ($sid)" "$out"
echo "DROP PROCEDURE demo_count; FLUSH PRIVILEGES;" | mysql_sql >/dev/null
out=$(do_restore "$(as_user mysqlmin bk "$bk_secret")" "$sid" app)
echo "$extras_sql" | mysql_sql | grep -qx "1|1|1"
restored "mysql restore of that backup keeps the procedure" "$out" $?
out=$(backup_now "$(as_user mysqlexec bk_exec "$exec_secret")"); sid=$(printf "%s" "$out" | sid_of)
shown=$(show_snapshot "$sid")
if printf "%s" "$out" | grep -q "finished: error" && printf "%s" "$shown" | grep -q "insufficient privileges"; then
  ok "mysql backup of a routine the user cannot read fails ($sid)"
  printf '%s\n' "$shown" | grep -o '"error": "[^"]*"' | head -1 | sed 's/^/         | /'
else
  ko "mysql backup of a routine the user cannot read fails ($sid)" "$out"$'\n'"$shown"
fi

# ── Filesystem (local files) ─────────────────────────────────────────────────
echo "== engine: filesystem =="
# Seed as root and hand /files to the non-root backup uid so it can read (for
# backup) and write (for restore). In production the restore target volume must
# likewise be writable by the container's uid.
$COMPOSE run --rm --user 0 --entrypoint sh backup -c \
  "rm -rf /files/* && mkdir -p /files/sub && echo hello-fs > /files/note.txt && echo nested > /files/sub/b.txt && chown -R 1000:1000 /files" >/dev/null 2>&1
fs='{"instance_name":"e2e","jobs":[{"name":"fs","sources":[{"type":"filesystem","name":"data","path":"/files"}],"destinations":[{"type":"local"},'"${dest/PFX/files}"']}]}'
out=$(backup_now "$fs"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "filesystem backup ($sid)" || ko "filesystem backup ($sid)" "$out"
archived files "$sid" filesystem
in_files "rm -rf /files/note.txt /files/sub" >/dev/null 2>&1
do_restore "$fs" "$sid" data >/dev/null 2>&1
out=$(in_files "cat /files/note.txt; cat /files/sub/b.txt")
echo "$out" | grep -q "hello-fs" && echo "$out" | grep -q "nested" \
  && ok "filesystem restore roundtrip" || ko "filesystem restore roundtrip" "$out"

# ── S3-bucket source (mirror with per-object metadata) ───────────────────────
echo "== engine: s3-bucket-source =="
# scripts/e2e_s3.py seeds and checks the objects: bodies, content headers
# (Content-Disposition & co.), user metadata and tags. It reuses the backup
# user's credentials from $dest.
E2E_S3_ACCESS_KEY=$(printf '%s' "$dest" | sed -E 's/.*"access_key":"([^"]*)".*/\1/')
E2E_S3_SECRET_KEY=$(printf '%s' "$dest" | sed -E 's/.*"secret_key":"([^"]*)".*/\1/')
export E2E_S3_ACCESS_KEY E2E_S3_SECRET_KEY
s3py(){ $COMPOSE run --rm -T -e E2E_S3_ACCESS_KEY -e E2E_S3_SECRET_KEY \
          --entrypoint python backup - "$@" < scripts/e2e_s3.py 2>&1; }
out=$(s3py seed) && ok "s3-source objects seeded" || ko "s3-source objects seeded" "$out"
s3='{"instance_name":"e2e","jobs":[{"name":"s3","sources":[{"type":"s3","name":"assets","endpoint":"http://minio:9000","bucket":"assets","access_key":"backup-app","secret_key":"backup-secret-dev","region":"eu-central-1","force_path_style":true}],"destinations":[{"type":"local"},'"${dest/PFX/s3mirror}"']}]}'
out=$(backup_now "$s3"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "s3-source backup ($sid)" || ko "s3-source backup ($sid)" "$out"
archived s3mirror "$sid" s3-source
s3py delete >/dev/null
out=$(do_restore "$s3" "$sid" assets)
check=$(s3py check) && ok "s3-source restore (bodies, content headers, metadata, tags)" \
  || ko "s3-source restore (bodies, content headers, metadata, tags)" "$check"$'\n'"$out"

# Credentials without s3:GetObjectTagging: the objects are still backed up, the
# job ends in warning, and they restore without tags.
notags_secret=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
notags_policy='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":["arn:aws:s3:::assets/*"]},{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::assets"]}]}'
out=$(mc "printf '%s' '$notags_policy' > /tmp/p.json && mc admin policy create m pNoTags /tmp/p.json && mc admin user add m backup-notags $notags_secret && mc admin policy attach m pNoTags --user backup-notags") \
  && ok "minio user without s3:GetObjectTagging created" || ko "minio user without s3:GetObjectTagging created" "$out"
s3_notags=$(printf '%s' "$s3" | sed -E 's/"name":"s3"/"name":"s3notags"/; s/"access_key":"[^"]*","secret_key":"[^"]*"/"access_key":"backup-notags","secret_key":"'"$notags_secret"'"/')
out=$(backup_now "$s3_notags"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: warning" && printf "%s" "$out" | grep -q "get_object_tagging: AccessDenied" \
  && ok "s3-source backup without tag permission: objects kept, warning ($sid)" \
  || ko "s3-source backup without tag permission ($sid)" "$out"
s3py delete >/dev/null
out=$(do_restore "$s3_notags" "$sid" assets)
check=$(s3py check-untagged) && ok "s3-source restore without tags" \
  || ko "s3-source restore without tags" "$check"$'\n'"$out"
