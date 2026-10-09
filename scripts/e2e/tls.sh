# shellcheck shell=bash
# =============================================================================
# e2e suite "tls" — sourced by scripts/e2e.sh (helpers, $COMPOSE, ok/ko)
# -----------------------------------------------------------------------------
# An S3 destination and an S3 source on MinIO served over HTTPS only, with a
# certificate of the runtime CA (scripts/e2e.sh, /pki/ca.crt):
#
#   * default verification, no ca_bundle: the endpoint is refused
#     (CERTIFICATE_VERIFY_FAILED); the snapshot stays local, run in warning;
#   * ca_bundle: the upload succeeds; with the local copy gone, list shows it
#     off-site, restore hydrates it over TLS and verify hydrates it again;
#   * verify_tls false: it works, and the run logs the security warning;
#   * an s3 source mirrors that bucket over TLS - refused without ca_bundle,
#     complete with it.
# =============================================================================

echo "== tls: MinIO over HTTPS with the runtime CA =="
E2E_MINIO_TLS_SECRET=$(rand_secret)   # MinIO's root password (docker-compose.e2e.yml)
export E2E_MINIO_TLS_SECRET
start_services minio-tls
TLS_DIR=/data/tls   # a data dir of its own (the suites may share the stack)
out=$($COMPOSE run --rm --user 0 --entrypoint sh backup -c \
  'rm -rf /files/tls && mkdir -p /files/tls && echo tls-data > /files/tls/data.txt &&
   chown -R 1000:1000 /files/tls' 2>&1) \
  && ok "tls: source seeded" || ko "tls: source seeded" "$out"

# The connection to minio-tls; $1 appends TLS fields. The secret stays a
# ${VAR} reference, resolved by the engine from its environment.
# shellcheck disable=SC2016
tls_conn(){ printf '"endpoint":"https://minio-tls:9000","access_key":"tls-admin","secret_key":"${E2E_MINIO_TLS_SECRET}","region":"eu-central-1","force_path_style":true%s' "$1"; }
# tls_dest_job <job> <TLS fields>: files -> local + S3 bucket backups-tls.
tls_dest_job(){ printf '{"instance_name":"e2e-tls","jobs":[{"name":"%s","sources":[{"type":"filesystem","name":"files","path":"/files/tls"}],"destinations":[{"type":"local"},{"type":"s3","bucket":"backups-tls",%s}]}]}' "$1" "$(tls_conn "$2")"; }
# tls_src_job <job> <TLS fields>: the bucket backups-tls as an s3 source -> local.
tls_src_job(){ printf '{"instance_name":"e2e-tls","jobs":[{"name":"%s","sources":[{"type":"s3","name":"mirror","bucket":"backups-tls",%s}]}]}' "$1" "$(tls_conn "$2")"; }
# tls_cli <config> <command...>: one engine command in the suite's data dir.
tls_cli(){ local cfg=$1; shift
           $COMPOSE run --rm -e BACKUP_DATA_DIR="$TLS_DIR" -e BACKUP_CONFIG_JSON="$cfg" \
             -e E2E_MINIO_TLS_SECRET backup "$@" 2>&1; }

# Default verification: boto3's CA bundle does not hold the runtime CA.
out=$(tls_cli "$(tls_dest_job tlsdefault '')" --now)
if printf '%s' "$out" | grep -q "finished: warning" && printf '%s' "$out" | grep -q "CERTIFICATE_VERIFY_FAILED"; then
  ok "tls: default verification refused the private CA (CERTIFICATE_VERIFY_FAILED), kept local"
else
  ko "tls: default verification refused the private CA" "$out"
fi

# ca_bundle: the CA file replaces the default bundle for this endpoint.
ca_job=$(tls_dest_job tlsca ',"ca_bundle":"/pki/ca.crt"')
out=$(tls_cli "$ca_job" --now); sid=$(printf '%s' "$out" | sid_of)
printf '%s' "$out" | grep -q "finished: success" \
  && ok "tls: backup to MinIO over HTTPS with ca_bundle ($sid)" \
  || ko "tls: backup to MinIO over HTTPS with ca_bundle ($sid)" "$out"
in_files "rm -f $TLS_DIR/$sid.* /files/tls/data.txt" >/dev/null
out=$(tls_cli "$ca_job" list)
printf '%s' "$out" | grep -q "$sid .*(off-site only)" \
  && ok "tls: list shows the snapshot off-site only" || ko "tls: list shows the snapshot off-site only" "$out"
out=$(tls_cli "$ca_job" restore "$sid" --force)
data=$(in_files "cat /files/tls/data.txt")
if printf '%s' "$out" | grep -q "hydrated snapshot $sid" && [ "$data" = "tls-data" ]; then
  restored "tls: restore hydrated the snapshot over HTTPS and restored the file" "$out" 0
else
  ko "tls: restore hydrated the snapshot over HTTPS (file: '$data')" "$out"
fi
in_files "rm -f $TLS_DIR/$sid.*" >/dev/null
out=$(tls_cli "$ca_job" verify "$sid")
printf '%s' "$out" | grep -q "^OK $sid" \
  && ok "tls: verify hydrated the snapshot over HTTPS, sha256 OK" || ko "tls: verify over HTTPS" "$out"

# verify_tls false: no verification at all, never silently.
out=$(tls_cli "$(tls_dest_job tlsinsecure ',"verify_tls":false')" --now)
if printf '%s' "$out" | grep -q "finished: success" \
   && printf '%s' "$out" | grep -q "TLS certificate verification is DISABLED for the S3 endpoint https://minio-tls:9000"; then
  ok "tls: verify_tls false uploads and logs the warning"
else
  ko "tls: verify_tls false uploads and logs the warning" "$out"
fi

# The s3 source shares the client: refused without ca_bundle, complete with it.
out=$(tls_cli "$(tls_src_job tlssrcdefault '')" --now)
printf '%s' "$out" | grep -q "finished: error" && printf '%s' "$out" | grep -q "CERTIFICATE_VERIFY_FAILED" \
  && ok "tls: s3 source refused the private CA without ca_bundle" \
  || ko "tls: s3 source refused the private CA without ca_bundle" "$out"
out=$(tls_cli "$(tls_src_job tlssrc ',"ca_bundle":"/pki/ca.crt"')" --now); sid=$(printf '%s' "$out" | sid_of)
shown=$($COMPOSE run --rm -e BACKUP_DATA_DIR="$TLS_DIR" backup show "$sid" 2>&1)
if printf '%s' "$out" | grep -q "finished: success" && printf '%s' "$shown" | grep -q '"object_count": [1-9]'; then
  ok "tls: s3 source mirrored the bucket over HTTPS with ca_bundle ($sid)"
else
  ko "tls: s3 source mirrored the bucket over HTTPS with ca_bundle ($sid)" "$out"$'\n'"$shown"
fi
