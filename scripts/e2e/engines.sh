# shellcheck shell=bash
# =============================================================================
# e2e suite "engines" — sourced by scripts/e2e.sh (helpers, $COMPOSE, ok/ko)
# -----------------------------------------------------------------------------
# A real backup -> (local + MinIO S3) -> restore round trip against every source
# engine of docker-compose.e2e.yml: each one is seeded, backed up, has its data
# destroyed, restored and asserted. The backups and restores run in the engine
# image itself, so they use its own clients (pg_dump/pg_restore,
# mariadb-dump/mariadb).
#
# E2E_MINIO=0 runs the suite without MinIO, for a platform the CS-MinIO images
# are not published for (linux/arm64): the S3 destination then has no bucket,
# which the engine skips as "not configured", and the MinIO checks and the
# S3-bucket source are reported as skipped.
# =============================================================================

E2E_MINIO="${E2E_MINIO:-1}"
# archived <prefix> <snapshot id> <label>: the snapshot reached the MinIO bucket.
archived(){
  if [ "$E2E_MINIO" = 0 ]; then skip "$3 archive in MinIO (E2E_MINIO=0)"; return; fi
  local listing; listing=$(mc "mc ls m/backups/$1/")
  printf '%s' "$listing" | grep -q "$2" && ok "$3 archive in MinIO" || ko "$3 archive in MinIO" "$listing"
}
# mc from the CS-MinIO init image (the minio/mc image is no longer published).
mc(){ docker run --rm --network bh-e2e --entrypoint sh ghcr.io/bauer-group/cs-minio/minio-init:latest -c \
        "mc alias set m http://minio:9000 admin minioadmin-dev >/dev/null 2>&1 && $1" 2>&1; }
# SQL from stdin, run in the server containers with the root password from
# their own environment (no credentials in this script).
psql_app(){ $COMPOSE exec -T postgres psql -U app -d app -v ON_ERROR_STOP=1 -X -q -tA "$@" 2>&1; }
# psql_in <database> [psql args]: the same in another database of the server.
psql_in(){ local db=$1; shift
           $COMPOSE exec -T postgres psql -U app -d "$db" -v ON_ERROR_STOP=1 -X -q -tA "$@" 2>&1; }
maria_sql(){ $COMPOSE exec -T mariadb sh -c \
        'exec mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" --default-character-set=utf8mb4 -N app' 2>&1; }
maria_app_sql(){ $COMPOSE exec -T mariadb sh -c \
        'exec mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" --default-character-set=utf8mb4 -N app' 2>&1; }
mysql_sql(){ $COMPOSE exec -T mysql sh -c \
        'exec mysql -uroot --password="$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 -N app' 2>&1 \
        | grep -v 'Using a password on the command line'; }

dest='{"type":"s3","endpoint":"http://minio:9000","bucket":"backups","access_key":"backup-app","secret_key":"backup-secret-dev","region":"eu-central-1","force_path_style":true,"ensure_bucket":false,"prefix":"PFX/"}'
[ "$E2E_MINIO" = 0 ] && dest='{"type":"s3","bucket":""}'

# ── bring up infra ───────────────────────────────────────────────────────────
echo "== start infra =="
if [ "$E2E_MINIO" = 0 ]; then
  $COMPOSE up -d postgres mariadb mysql >/dev/null 2>&1
else
  $COMPOSE up -d postgres mariadb mysql minio minio-init >/dev/null 2>&1
fi

echo "== wait for databases + minio-init =="
for _ in $(seq 1 30); do
  ph=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_POSTGRES 2>/dev/null)
  mh=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_MARIADB 2>/dev/null)
  yh=$(docker inspect -f '{{.State.Health.Status}}' bh-e2e_MYSQL 2>/dev/null)
  ii=$(docker inspect -f '{{.State.Status}}:{{.State.ExitCode}}' bh-e2e_MINIO_INIT 2>/dev/null)
  [ "$E2E_MINIO" = 0 ] && ii="exited:0"   # not started (E2E_MINIO=0)
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
# pg_state [database]: "<demo name>|<event ids>|<partitions>|<runtime SELECT on
# demo>|<runtime INSERT on events>" of the database (default: app)
pg_state(){ psql_in "${1:-app}" <<'SQL'
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

# (d) the database owner, not a superuser: an application's own role backs up
# and restores its database - the partitioned table and keep_acl included. The
# engine logs in over the network with the password built here; the seed runs
# as that role over the server's local socket.
owner_secret=$(rand_secret)
out=$(psql_app <<SQL
DROP DATABASE IF EXISTS app_owned;
DROP ROLE IF EXISTS app_owner;
CREATE ROLE app_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$owner_secret';
CREATE DATABASE app_owned OWNER app_owner;
SQL
)
psql_owner(){ $COMPOSE exec -T postgres psql -U app_owner -d app_owned -v ON_ERROR_STOP=1 -X -q -tA "$@" 2>&1; }
out+=$(psql_owner <<'SQL'
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
)
st=$(psql_app -c "SELECT concat_ws('|', rolsuper, rolcreatedb, rolcreaterole, rolbypassrls)
                  FROM pg_roles WHERE rolname = 'app_owner'")
[ "$st" = "f|f|f|f" ] && [ "$(pg_state app_owned)" = "e2e-original|1,2|2|t|t" ] \
  && ok "postgres database owner app_owner seeded, not a superuser" \
  || ko "postgres database owner app_owner seeded: got '$st'" "$out"
pg_owner='{"instance_name":"e2e","jobs":[{"name":"pgowner","sources":[{"type":"postgres","host":"postgres","database":"app_owned","user":"app_owner","password":"'"$owner_secret"'","keep_acl":true}],"destinations":[{"type":"local"},'"${dest/PFX/pgowner}"']}]}'
out=$(backup_now "$pg_owner"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" \
  && ok "postgres backup as the database owner ($sid)" \
  || ko "postgres backup as the database owner ($sid)" "$out"
archived pgowner "$sid" "postgres (database owner)"
psql_owner -c "DROP TABLE demo, events;" >/dev/null
out=$(do_restore "$pg_owner" "$sid" app_owned); st=$(pg_state app_owned)
[ "$st" = "e2e-original|1,2|2|t|t" ] \
  && ok "postgres restore as the database owner into an empty database" \
  || ko "postgres restore as the database owner into an empty database: got '$st'" "$out"
psql_owner -c "UPDATE demo SET name = 'mutated' WHERE id = 1; DELETE FROM events WHERE id = 1;
               INSERT INTO events VALUES (3, '2026-07-01', 'after-backup');
               REVOKE ALL ON demo, events FROM app_runtime;" >/dev/null
out=$(do_restore "$pg_owner" "$sid" app_owned); st=$(pg_state app_owned)
[ "$st" = "e2e-original|1,2|2|t|t" ] \
  && ok "postgres restore as the database owner over a live partitioned table keeps the grants" \
  || ko "postgres restore as the database owner over a live partitioned table: got '$st'" "$out"

# (e) a schema that moved on after the snapshot: an application upgrade migrated
# the database, then the snapshot from before it is restored over it - n8n's
# workflow_suggestion, a foreign key onto workflow_entity's unique index. Alone,
# pg_restore --clean fails ("cannot drop index pk_workflow_id because other
# objects depend on it"); the restore replaces the snapshot's schemas, and what
# the upgrade added there is gone, so the application migrates again. As the
# database owner, with identity columns, a schema of the application's own and a
# large object in the snapshot.
out=$(psql_app <<'SQL'
DROP DATABASE IF EXISTS app_migrated;
CREATE DATABASE app_migrated OWNER app_owner;
SQL
)
psql_migrated(){ $COMPOSE exec -T postgres psql -U app_owner -d app_migrated -v ON_ERROR_STOP=1 -X -q -tA "$@" 2>&1; }
out+=$(psql_migrated <<'SQL'
CREATE TABLE workflow(id varchar(36) NOT NULL, name text);
CREATE UNIQUE INDEX pk_workflow_id ON workflow(id);
CREATE TABLE shared(workflow_id varchar(36) NOT NULL REFERENCES workflow(id), role text);
CREATE DOMAIN label AS text CHECK (VALUE <> '');
CREATE TABLE tag(id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name label);
CREATE TABLE migrations(id serial PRIMARY KEY, name text NOT NULL);
CREATE SCHEMA audit;
CREATE TABLE audit.log(id int GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                       workflow_id varchar(36) REFERENCES workflow(id));
INSERT INTO workflow VALUES ('w1', 'snapshot');
INSERT INTO shared VALUES ('w1', 'owner');
INSERT INTO tag(name) VALUES ('prod');
INSERT INTO migrations(name) VALUES ('Init');
INSERT INTO audit.log(workflow_id) VALUES ('w1');
SELECT lo_from_bytea(0, 'snapshot-lo') > 0;
SQL
)
# migrated_state: "<tables and views of the snapshot's schemas>|<workflow names>|
# <migrations>|<large objects>|<enum types>|<functions in audit>"
migrated_state(){ psql_migrated <<'SQL'
SELECT concat_ws('|',
  (SELECT string_agg(n.nspname || '.' || c.relname, ',' ORDER BY n.nspname, c.relname) FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('public', 'audit') AND c.relkind IN ('r', 'v')),
  (SELECT string_agg(name, ',' ORDER BY id) FROM workflow),
  (SELECT string_agg(name, ',' ORDER BY id) FROM migrations),
  (SELECT string_agg(convert_from(lo_get(oid), 'UTF8'), ',' ORDER BY oid) FROM pg_largeobject_metadata),
  (SELECT coalesce(string_agg(n.nspname || '.' || t.typname, ',' ORDER BY n.nspname, t.typname), '-') FROM pg_type t
     JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname IN ('public', 'audit') AND t.typtype = 'e'),
  (SELECT coalesce(string_agg(p.proname, ',' ORDER BY p.proname), '-') FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'audit'));
SQL
}
# migrate: the upgrade - an enum type and a table using it with a foreign key
# onto workflow's unique index, a view on it, a range type, a new key between
# two of the snapshot's tables, a type and a function in the application's own
# schema, a report in a schema of its own on top of the snapshot's table, a
# migration row, changed rows and a changed large object.
migrate(){ psql_migrated <<'SQL'
CREATE TYPE suggestion_kind AS ENUM ('auto', 'manual');
CREATE TYPE suggestion_window AS RANGE (subtype = int4);
CREATE TABLE workflow_suggestion(id int GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                                 kind suggestion_kind, workflow_id varchar(36) REFERENCES workflow(id));
INSERT INTO workflow_suggestion(kind, workflow_id) VALUES ('auto', 'w1');
CREATE VIEW suggestion_count AS SELECT count(*) FROM workflow_suggestion;
ALTER TABLE tag ADD COLUMN workflow_id varchar(36) REFERENCES workflow(id);
CREATE TYPE audit.level AS ENUM ('info', 'warn');
CREATE FUNCTION audit.touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
CREATE SCHEMA IF NOT EXISTS reporting;
CREATE VIEW reporting.workflow_names AS SELECT name FROM workflow;
INSERT INTO migrations(name) VALUES ('AddWorkflowSuggestion');
UPDATE workflow SET name = 'migrated';
SELECT lo_put(oid, 0, 'migrated') FROM pg_largeobject_metadata;
SQL
}
snap="audit.log,public.migrations,public.shared,public.tag,public.workflow|snapshot|Init|snapshot-lo|-|-"
st=$(migrated_state)
[ "$st" = "$snap" ] && ok "postgres database app_migrated seeded" \
  || ko "postgres database app_migrated seeded: got '$st'" "$out"
pg_mig='{"instance_name":"e2e","jobs":[{"name":"pgmig","sources":[{"type":"postgres","host":"postgres","database":"app_migrated","user":"app_owner","password":"'"$owner_secret"'"}],"destinations":[{"type":"local"}]}]}'
out=$(backup_now "$pg_mig"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" \
  && ok "postgres backup of app_migrated ($sid)" || ko "postgres backup of app_migrated ($sid)" "$out"
# (e1) the snapshot's own schema, its rows changed: still the plain pg_restore
psql_migrated -c "UPDATE workflow SET name = 'mutated'; INSERT INTO migrations(name) VALUES ('Later');" >/dev/null
out=$(do_restore "$pg_mig" "$sid" app_migrated); st=$(migrated_state)
if [ "$st" = "$snap" ] && ! printf '%s' "$out" | grep -q "by replacing"; then
  ok "postgres restore over the snapshot's own schema stays the plain pg_restore"
else ko "postgres restore over the snapshot's own schema: got '$st'" "$out"; fi
# (e2) over the migrated database: the snapshot comes back, what the upgrade
# added to its schemas goes - named in the log, the report CASCADE took along
# too. reporting.kpi, outside the snapshot's schemas, keeps its rows and its
# column of its own enum; only its key onto the snapshot's table goes.
out=$(psql_migrated <<'SQL'
CREATE SCHEMA reporting;
CREATE TYPE reporting.grade AS ENUM ('a', 'b');
CREATE TABLE reporting.kpi(workflow_id varchar(36) REFERENCES workflow(id), grade reporting.grade);
INSERT INTO reporting.kpi VALUES ('w1', 'a');
SQL
)
migrate >/dev/null
out+=$(do_restore "$pg_mig" "$sid" app_migrated); st=$(migrated_state)
kpi=$(psql_migrated -c "SELECT grade || '|' || (SELECT count(*) FROM pg_constraint
                        WHERE conrelid = 'reporting.kpi'::regclass) FROM reporting.kpi")
absent="not in the snapshot, dropped: public.workflow_suggestion, public.suggestion_count, type audit.level, type public.suggestion_kind, type public.suggestion_window, foreign key tag_workflow_id_fkey on public.tag, foreign key kpi_workflow_id_fkey on reporting.kpi"
if [ "$st" = "$snap" ] && [ "$kpi" = "a|0" ] && printf '%s' "$out" | grep -qF "$absent" \
   && printf '%s' "$out" | grep -q "drop cascades to view reporting.workflow_names"; then
  ok "postgres restore over a schema a newer application version migrated"
else ko "postgres restore over a migrated schema: got '$st', reporting.kpi '$kpi'" "$out"; fi
# (e3) a restore that fails late - a privilege for a role that is gone, applied
# after the large object - leaves the migrated database exactly as it was: the
# drops roll back with everything else.
out=$(psql_app -c "DROP ROLE IF EXISTS app_ghost; CREATE ROLE app_ghost NOLOGIN;")
out+=$(psql_migrated -c "GRANT SELECT ON tag TO app_ghost;")
pg_mig_acl=$(printf '%s' "$pg_mig" | sed 's/"name":"pgmig"/"name":"pgmigacl"/; s/"password":/"keep_acl":true,"password":/')
out+=$(backup_now "$pg_mig_acl"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" \
  && ok "postgres backup of app_migrated with keep_acl ($sid)" \
  || ko "postgres backup of app_migrated with keep_acl ($sid)" "$out"
migrate >/dev/null; before=$(migrated_state)
psql_migrated -c "REVOKE ALL ON tag FROM app_ghost;" >/dev/null; psql_app -c "DROP ROLE app_ghost;" >/dev/null
out=$(do_restore "$pg_mig_acl" "$sid" app_migrated); st=$(migrated_state)
if printf '%s' "$out" | grep -q 'role "app_ghost" does not exist' && [ "$st" = "$before" ] \
   && [ "$st" != "$snap" ]; then
  ok "a failing restore leaves the migrated database as it was, past the large object"
else ko "a failing restore over a migrated schema: got '$st', expected '$before'" "$out"; fi

# (f) an extension's own objects are no difference from the snapshot - its
# member view (pg_buffercache), composite types (tablefunc) and domains
# (earthdistance) are not in the dump, CREATE EXTENSION brings them back: the
# restore stays the plain pg_restore. As the superuser, who owns the extensions.
out=$(psql_app <<'SQL'
DROP DATABASE IF EXISTS app_ext;
CREATE DATABASE app_ext;
SQL
)
out+=$(psql_in app_ext <<'SQL'
CREATE EXTENSION pg_buffercache;
CREATE EXTENSION tablefunc;
CREATE EXTENSION earthdistance CASCADE;
CREATE TABLE note(id int PRIMARY KEY, body text);
INSERT INTO note VALUES (1, 'snapshot');
SQL
)
pg_ext='{"instance_name":"e2e","jobs":[{"name":"pgext","sources":[{"type":"postgres","host":"postgres","database":"app_ext","user":"app","password":"devpassword"}],"destinations":[{"type":"local"}]}]}'
out+=$(backup_now "$pg_ext"); sid=$(printf "%s" "$out" | sid_of)
psql_in app_ext -c "UPDATE note SET body = 'mutated';" >/dev/null
out+=$(do_restore "$pg_ext" "$sid" app_ext); st=$(psql_in app_ext -c "SELECT body FROM note")
if [ "$st" = "snapshot" ] && printf '%s' "$out" | grep -q "restore complete" \
   && ! printf '%s' "$out" | grep -q "by replacing"; then
  ok "postgres restore beside an extension's relations stays the plain pg_restore"
else ko "postgres restore beside an extension's relations: got '$st'" "$out"; fi

# ── MariaDB ──────────────────────────────────────────────────────────────────
echo "== engine: mariadb =="
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e \
  "DROP TABLE IF EXISTS demo; CREATE TABLE demo(id int PRIMARY KEY, name varchar(64)); INSERT INTO demo VALUES (1,'e2e-original');" 2>/dev/null
# 4-byte UTF-8 (--default-character-set=utf8mb4), a trigger (--triggers), a
# stored procedure (--routines) and an event (--events), created by the
# application user like an application would: the dump keeps their DEFINER.
# The event is disabled and scheduled in the future, so it never runs.
extras_seed="ALTER TABLE demo CONVERT TO CHARACTER SET utf8mb4;
INSERT INTO demo VALUES (2, 'utf8mb4 😀 ü');
CREATE TRIGGER demo_bi BEFORE INSERT ON demo FOR EACH ROW SET NEW.name = TRIM(NEW.name);
DROP PROCEDURE IF EXISTS demo_count;
CREATE PROCEDURE demo_count(OUT n INT) SELECT COUNT(*) INTO n FROM demo;
DROP EVENT IF EXISTS demo_tick;
CREATE EVENT demo_tick ON SCHEDULE EVERY 1 DAY STARTS '2030-01-01 00:00:00' DISABLE
  DO UPDATE demo SET name = name WHERE id = 0;"
echo "$extras_seed" | maria_app_sql
# The routine and event a restore must bring back (the trigger goes with its table).
extras_drop="DROP PROCEDURE IF EXISTS demo_count; DROP EVENT IF EXISTS demo_tick;"
# "<4-byte text intact>|<trigger present>|<procedure present>|<event present>"
extras_sql="SELECT CONCAT((SELECT name FROM demo WHERE id = 2) = 'utf8mb4 😀 ü', '|',
  (SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'app' AND TRIGGER_NAME = 'demo_bi'), '|',
  (SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'app' AND ROUTINE_NAME = 'demo_count'), '|',
  (SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = 'app' AND EVENT_NAME = 'demo_tick'));"
maria='{"instance_name":"e2e","jobs":[{"name":"maria","sources":[{"type":"mariadb","host":"mariadb","database":"app","user":"app","password":"devpassword"}],"destinations":[{"type":"local"},'"${dest/PFX/maria}"']}]}'
out=$(backup_now "$maria"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" && ok "mariadb backup ($sid)" || ko "mariadb backup ($sid)" "$out"
archived maria "$sid" mariadb
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e "DROP TABLE demo;" 2>/dev/null
echo "$extras_drop" | maria_sql >/dev/null
out=$(do_restore "$maria" "$sid" app)
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -N -e "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original"
restored "mariadb restore roundtrip" "$out" $?
st=$(echo "$extras_sql" | maria_sql)
[ "$st" = "1|1|1|1" ] && ok "mariadb restore keeps utf8mb4 text, trigger, procedure and event" \
  || ko "mariadb utf8mb4 text, trigger, procedure and event: got '$st'"

# Least-privilege backup users with the grants docs/sources.md lists: bk_min
# holds SELECT, SHOW VIEW, TRIGGER and EVENT on the database plus SHOW CREATE
# ROUTINE (MariaDB 11.3+) to read the routine bodies - no LOCK TABLES, no
# global privilege. bk_exec sees the procedure only through EXECUTE, so MariaDB
# hides its body: that backup must fail, not succeed without it. The restores
# run as the application user, the definer of every object.
maria_min_secret=$(rand_secret)
maria_exec_secret=$(rand_secret)
out=$(maria_sql <<SQL
DROP USER IF EXISTS 'bk_min'@'%', 'bk_exec'@'%';
CREATE USER 'bk_min'@'%' IDENTIFIED BY '$maria_min_secret';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, SHOW CREATE ROUTINE ON app.* TO 'bk_min'@'%';
CREATE USER 'bk_exec'@'%' IDENTIFIED BY '$maria_exec_secret';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, EXECUTE ON app.* TO 'bk_exec'@'%';
SQL
)
[ -z "$out" ] && ok "mariadb least-privilege users created" || ko "mariadb least-privilege users created" "$out"
echo "  bk_min: $(echo "SHOW GRANTS FOR 'bk_min'@'%';" | maria_sql | tail -1)"
# maria_as <job> <user> <password>: the mariadb job under another name and login.
maria_as(){ printf '%s' "$maria" | sed -E 's/"name":"maria"/"name":"'"$1"'"/;
              s/"user":"app","password":"[^"]*"/"user":"'"$2"'","password":"'"$3"'"/'; }
out=$(backup_now "$(maria_as mariamin bk_min "$maria_min_secret")"); sid=$(printf "%s" "$out" | sid_of)
printf "%s" "$out" | grep -q "finished: success" \
  && ok "mariadb backup as a least-privilege user ($sid)" \
  || ko "mariadb backup as a least-privilege user ($sid)" "$out"
$COMPOSE exec -T mariadb mariadb -uroot -prootpw app -e "DROP TABLE demo;" 2>/dev/null
echo "$extras_drop" | maria_sql >/dev/null
out=$(do_restore "$maria" "$sid" app)
echo "$extras_sql" | maria_sql | grep -qx "1|1|1|1"
restored "mariadb restore of that backup keeps trigger, procedure and event" "$out" $?
out=$(backup_now "$(maria_as mariaexec bk_exec "$maria_exec_secret")"); sid=$(printf "%s" "$out" | sid_of)
shown=$(show_snapshot "$sid")
if printf "%s" "$out" | grep -q "finished: error" && printf "%s" "$shown" | grep -q "insufficient privileges"; then
  ok "mariadb backup of a routine the user cannot read fails ($sid)"
  printf '%s\n' "$shown" | grep -o '"error": "[^"]*"' | head -1 | sed 's/^/         | /'
else
  ko "mariadb backup of a routine the user cannot read fails ($sid)" "$out"$'\n'"$shown"
fi

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
echo "$extras_drop FLUSH PRIVILEGES;" | mysql_sql >/dev/null
out=$(do_restore "$mysql" "$sid" app)
$COMPOSE exec -T mysql mysql -uroot -prootpw app -N -e "SELECT name FROM demo WHERE id=1;" 2>/dev/null | grep -q "e2e-original"
restored "mysql restore roundtrip" "$out" $?
st=$(echo "$extras_sql" | mysql_sql)
[ "$st" = "1|1|1|1" ] && ok "mysql restore keeps utf8mb4 text, trigger, procedure and event" \
  || ko "mysql utf8mb4 text, trigger, procedure and event: got '$st'"

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
echo "$extras_drop FLUSH PRIVILEGES;" | mysql_sql >/dev/null
out=$(do_restore "$(as_user mysqlmin bk "$bk_secret")" "$sid" app)
echo "$extras_sql" | mysql_sql | grep -qx "1|1|1|1"
restored "mysql restore of that backup keeps the procedure and the event" "$out" $?
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
if [ "$E2E_MINIO" = 0 ]; then
  skip "s3-bucket source round trips (E2E_MINIO=0: no MinIO)"
  return 0   # the last section: end this suite, the runner goes on
fi
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
