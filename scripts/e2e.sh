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
#   alerts    every alert channel delivered to real receivers: an HTTP sink
#             (signed webhook, Teams, Slack, Discord, ntfy, Healthchecks) and
#             SMTP servers with STARTTLS and SMTPS; levels, escaping of run
#             data, an untrusted certificate, a receiver that never answers.
#   multijob  two jobs in one data dir under the scheduler daemon: same-second
#             start, job-scoped ids, per-job retention and prune, restore by a
#             job-scoped id, SIGTERM drain, the per-job healthcheck.
#   tls       S3 destination and source on MinIO over HTTPS with a private CA:
#             refused by default, ca_bundle (incl. hydration on restore and
#             verify), verify_tls false and its warning.
#   encryption  age and gpg with keys generated in the run: back up encrypted,
#             verify, restore; failures stored UNENCRYPTED with a warning.
#
# TLS endpoints use a CA generated per run with openssl (.e2e-pki/, mounted
# read-only at /pki); no key material is kept in the repository.
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
ALL_SUITES="engines alerts multijob tls encryption"
PKI=".e2e-pki"

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

PASS=0; FAIL=0; SKIPPED=0
ok(){ echo "  [PASS] $1"; PASS=$((PASS+1)); }
skip(){ echo "  [SKIP] $1"; SKIPPED=$((SKIPPED+1)); }
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
  [ "$KEEP" = "--keep" ] || {
    echo "== teardown =="; $COMPOSE down -v >/dev/null 2>&1; rm -rf "$OVERRIDE" "$PKI"
  }
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
# tally <output> <exit code>: count the "PASS <label>" / "FAIL <label>: ..."
# lines of a python check helper; other lines are shown as they are. A helper
# that failed without a FAIL line (a traceback) counts as one failure.
tally(){
  local line fails=0
  while IFS= read -r line; do
    case "$line" in
      "PASS "*) ok "${line#PASS }" ;;
      "FAIL "*) ko "${line#FAIL }"; fails=$((fails+1)) ;;
      "") ;;
      *) echo "         | $line" ;;
    esac
  done <<< "$1"
  if [ "$2" != 0 ] && [ "$fails" = 0 ]; then ko "check helper exited $2 without a FAIL line"; fi
}
# start_services <service>...: start them and wait until each is healthy (or
# running, without a healthcheck); a service that is not ready in 2 min fails.
start_services(){
  local out svc id state pending
  out=$($COMPOSE up -d "$@" 2>&1) || { ko "start $*" "$out"; return 1; }
  for _ in $(seq 1 40); do
    pending=""
    for svc in "$@"; do
      id=$($COMPOSE ps -q "$svc" 2>/dev/null)
      state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null)
      case "$state" in healthy|running) ;; *) pending="$pending $svc=${state:-missing}" ;; esac
    done
    [ -z "$pending" ] && { echo "  ready: $*"; return 0; }
    sleep 3
  done
  ko "services ready: $*" "still waiting for:$pending"
  return 1
}

# pki_init: a throw-away CA and one server certificate, issued for every TLS
# endpoint of the suites, valid two days. Python's default context
# (VERIFY_X509_STRICT) wants the CA's key usage and the leaf's key identifiers.
pki_init(){
  rm -rf "$PKI" && mkdir -p "$PKI/minio/CAs" || return 1
  cat > "$PKI/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
x509_extensions = ca_ext
[dn]
CN = BackupHelper e2e CA
[ca_ext]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF
  cat > "$PKI/server.ext" <<'EOF'
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
subjectAltName = DNS:minio-tls,DNS:mail-starttls,DNS:mail-smtps
EOF
  openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
      -config "$PKI/ca.cnf" -keyout "$PKI/ca.key" -out "$PKI/ca.crt" \
    && openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
      -subj "/CN=backuphelper-e2e" -keyout "$PKI/server.key" -out "$PKI/server.csr" \
    && openssl x509 -req -in "$PKI/server.csr" -CA "$PKI/ca.crt" -CAkey "$PKI/ca.key" \
      -set_serial "0x$(rand_secret)" -days 2 -sha256 -extfile "$PKI/server.ext" -out "$PKI/server.crt" \
    || return 1
  cp "$PKI/server.crt" "$PKI/minio/public.crt" && cp "$PKI/server.key" "$PKI/minio/private.key"
  rm -f "$PKI/ca.key" "$PKI/server.csr"
  # The servers and the engine read them as their own non-root users.
  chmod -R a+rX "$PKI"
}

# ── build the engine image (its Dockerfile runs the pytest gate) ─────────────
echo "== build backup image =="
if ! build_out=$($COMPOSE build backup 2>&1); then
  BUILD_FAILED=1; echo "build failed"; printf '%s\n' "$build_out" | tail -n 60; exit 1
fi
echo "== runtime PKI =="
if ! pki_out=$(pki_init 2>&1); then
  ko "runtime CA and server certificate" "$pki_out"; exit 1
fi
echo "  $(openssl x509 -in "$PKI/server.crt" -noout -subject -ext subjectAltName 2>/dev/null | tr '\n' ' ')"

for suite in "${SUITES[@]}"; do
  echo ""
  echo "######## suite: $suite ########"
  # shellcheck source=/dev/null
  . "scripts/e2e/$suite.sh"
done

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
echo "== E2E result (${SUITES[*]}): $PASS passed, $FAIL failed, $SKIPPED skipped =="
[ "$FAIL" -eq 0 ]
