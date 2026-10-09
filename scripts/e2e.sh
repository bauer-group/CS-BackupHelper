#!/usr/bin/env bash
# =============================================================================
# BackupHelper — end-to-end test suites
# -----------------------------------------------------------------------------
# Real backup -> restore round trips through the engine image itself, against
# the real servers of docker-compose.e2e.yml. The work is split into suites,
# one file each in scripts/e2e/; a run executes the named suites in order on
# one stack and prints one summary.
#
#   engines   (default) PostgreSQL, MariaDB, MySQL, filesystem and S3-bucket
#             sources: seed, back up (local + MinIO S3), destroy, restore and
#             assert, with the image's own clients (pg_dump/pg_restore,
#             mariadb-dump/mariadb).
#
#   ./scripts/e2e.sh                     # the engines suite, tear down at the end
#   ./scripts/e2e.sh <suite>...          # the named suites; "all" runs every suite
#   ./scripts/e2e.sh --keep <suite>...   # leave the stack running for inspection
#
# E2E_SUITES (space separated) names the suites when no argument does.
#
# The database server images come from docker-compose.e2e.yml (Dependabot keeps
# them current). To run against others without editing it, set any of
#   E2E_POSTGRES_IMAGE=postgres:17  E2E_MARIADB_IMAGE=mariadb:11.4  E2E_MYSQL_IMAGE=mysql:8.4
# CI (docker-release.yml) runs the suites this way.
# =============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1   # keep /paths literal for git-bash on Windows

cd "$(dirname "$0")/.." || exit 1
COMPOSE="docker compose -f docker-compose.e2e.yml"
ALL_SUITES="engines"

KEEP=""
SUITES=()
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP="--keep" ;;
    all) read -ra all <<< "$ALL_SUITES"; SUITES+=("${all[@]}") ;;
    *) SUITES+=("$arg") ;;
  esac
done
if [ "${#SUITES[@]}" -eq 0 ]; then
  read -ra SUITES <<< "${E2E_SUITES:-engines}"
  [ "${SUITES[*]}" = all ] && read -ra SUITES <<< "$ALL_SUITES"
fi
for suite in "${SUITES[@]}"; do
  [ -f "scripts/e2e/$suite.sh" ] || { echo "unknown suite '$suite' (suites: $ALL_SUITES, all)"; exit 2; }
done

PASS=0; FAIL=0
ok(){ echo "  [PASS] $1"; PASS=$((PASS+1)); }
# ko <label> [output]: a failed check, with the tail of the command output.
ko(){
  echo "  [FAIL] $1"; FAIL=$((FAIL+1))
  if [ -n "${2:-}" ]; then printf '%s\n' "$2" | tail -n 30 | sed 's/^/         | /'; fi
}

# Server images swapped in by E2E_*_IMAGE go into a generated override file.
OVERRIDE=".e2e-images.yml"
rm -f "$OVERRIDE"
if [ -n "${E2E_POSTGRES_IMAGE:-}${E2E_MARIADB_IMAGE:-}${E2E_MYSQL_IMAGE:-}" ]; then
  {
    echo "services:"
    if [ -n "${E2E_POSTGRES_IMAGE:-}" ]; then printf '  postgres:\n    image: %s\n' "$E2E_POSTGRES_IMAGE"; fi
    if [ -n "${E2E_MARIADB_IMAGE:-}" ]; then printf '  mariadb:\n    image: %s\n' "$E2E_MARIADB_IMAGE"; fi
    if [ -n "${E2E_MYSQL_IMAGE:-}" ]; then printf '  mysql:\n    image: %s\n' "$E2E_MYSQL_IMAGE"; fi
  } > "$OVERRIDE"
  COMPOSE="$COMPOSE -f $OVERRIDE"
fi

cleanup(){
  if [ "$FAIL" -gt 0 ] || [ "${BUILD_FAILED:-}" = 1 ]; then
    echo "== service logs (tail) =="
    $COMPOSE logs --no-color --tail 60 2>&1 | sed 's/^/  /'
  fi
  [ "$KEEP" = "--keep" ] || { echo "== teardown =="; $COMPOSE down -v >/dev/null 2>&1; rm -f "$OVERRIDE"; }
}
trap cleanup EXIT

# ── helpers shared by the suites ─────────────────────────────────────────────
backup_now(){ $COMPOSE run --rm -e BACKUP_CONFIG_JSON="$1" backup --now 2>&1; }
sid_of(){ grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}' | head -1; }
do_restore(){ $COMPOSE run --rm -e BACKUP_CONFIG_JSON="$1" backup restore "$2" --only "$3" --force 2>&1; }
show_snapshot(){ $COMPOSE run --rm backup show "$1" 2>&1; }
# restored <label> <restore output> <condition result>: the restore command
# itself reported success and the data check passed.
restored(){
  if printf '%s' "$2" | grep -q "restore complete" && [ "$3" = 0 ]; then ok "$1"; else ko "$1" "$2"; fi
}
in_files(){ $COMPOSE run --rm --entrypoint sh backup -c "$1" 2>&1; }
# rand_secret: a throw-away credential, built at runtime (none in the repo).
rand_secret(){ od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }

# ── build the engine image (its Dockerfile runs the pytest gate) ─────────────
echo "== build backup image =="
if ! build_out=$($COMPOSE build backup 2>&1); then
  BUILD_FAILED=1; echo "build failed"; printf '%s\n' "$build_out" | tail -n 60; exit 1
fi

for suite in "${SUITES[@]}"; do
  echo ""
  echo "######## suite: $suite ########"
  # shellcheck source=/dev/null
  . "scripts/e2e/$suite.sh"
done

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
echo "== E2E result (${SUITES[*]}): $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
