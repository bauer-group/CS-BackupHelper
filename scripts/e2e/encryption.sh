# shellcheck shell=bash
# =============================================================================
# e2e suite "encryption" — sourced by scripts/e2e.sh (helpers, $COMPOSE, ok/ko)
# -----------------------------------------------------------------------------
# Client-side encryption end to end, with keys generated in the run by the
# image's own age and gpg (volume e2e-keys at /keys; nothing in the repo):
#
#   age   back up to the public recipient, verify, restore with the identity
#         file; a restore without the identity must fail clearly
#   gpg   back up with a keyring that holds only the public key - imported,
#         never trusted, as on a backup host that cannot decrypt - verify,
#         restore with the keyring that holds the secret key
#   fail  a recipient age cannot use and a key the keyring lacks: the snapshot
#         is stored UNENCRYPTED, the run ends in warning and says so
# =============================================================================

echo "== encryption: keys generated in the run =="
ENC_DIR=/data/encryption   # a data dir of its own (the suites may share the stack)
out=$($COMPOSE run --rm --user 0 --entrypoint sh backup -c \
  'rm -rf /keys/* /files/enc && mkdir -p /files/enc && echo enc-data > /files/enc/data.txt &&
   chown -R 1000:1000 /keys /files/enc && chmod 700 /keys' 2>&1) \
  && ok "encryption: source and key volume prepared" || ko "encryption: source and key volume prepared" "$out"
# gpg-holder holds the key pair (where restores run), gpg-backup only the
# public key: imported, never signed or trusted.
keys=$(in_files 'set -e
  age-keygen -o /keys/age-identity.txt 2>/dev/null
  age-keygen -y /keys/age-identity.txt
  mkdir -m 700 /keys/gpg-holder /keys/gpg-backup
  export GNUPGHOME=/keys/gpg-holder
  gpg --batch --pinentry-mode loopback --passphrase "" \
      --quick-gen-key "BackupHelper e2e <e2e@backuphelper.test>" future-default default never 2>/dev/null
  fpr=$(gpg --batch --with-colons --list-keys | awk -F: "/^fpr/ {print \$10; exit}")
  echo "$fpr"
  gpg --batch --armor --export "$fpr" > /keys/gpg-public.asc
  gpgconf --kill gpg-agent
  GNUPGHOME=/keys/gpg-backup gpg --batch --import /keys/gpg-public.asc 2>/dev/null
  GNUPGHOME=/keys/gpg-backup gpgconf --kill gpg-agent')
age_rcpt=$(printf '%s\n' "$keys" | grep -oE '^age1[0-9a-z]+$' | head -1)
gpg_fpr=$(printf '%s\n' "$keys" | grep -oE '^[0-9A-F]{40}$' | head -1)
[ -n "$age_rcpt" ] && [ -n "$gpg_fpr" ] \
  && ok "encryption: age identity and gpg key pair generated (gpg $gpg_fpr)" \
  || ko "encryption: keys generated" "$keys"

# enc_job <job> <encryption block>: /files/enc -> local, encrypted.
enc_job(){ printf '{"instance_name":"e2e-enc","jobs":[{"name":"%s","sources":[{"type":"filesystem","name":"files","path":"/files/enc"}],"encryption":%s}]}' "$1" "$2"; }
# enc_cli <GNUPGHOME> <config> <command...>: one engine command in the suite's data dir.
enc_cli(){ local home=$1 cfg=$2; shift 2
           $COMPOSE run --rm -e BACKUP_DATA_DIR="$ENC_DIR" -e GNUPGHOME="$home" \
             -e BACKUP_CONFIG_JSON="$cfg" backup "$@" 2>&1; }
# stored <sid>: the files of the snapshot in the data dir, then the artifact's first bytes.
stored(){ in_files "cd $ENC_DIR && ls $1.* && for f in $1.tar.gz*; do head -c 21 \"\$f\" | od -An -c | tr -d ' \n'; echo; done"; }

echo "== encryption: age =="
age_enc='{"mode":"age","recipient":"'"$age_rcpt"'","identity_file":"/keys/age-identity.txt"}'
age_job=$(enc_job encage "$age_enc")
out=$(enc_cli /keys/gpg-backup "$age_job" --now); sid=$(printf '%s' "$out" | sid_of)
files=$(stored "$sid")
if printf '%s' "$out" | grep -q "finished: success" && printf '%s' "$files" | grep -qx "$sid.tar.gz.age" \
   && ! printf '%s' "$files" | grep -qx "$sid.tar.gz" && printf '%s' "$files" | grep -q "age-encryption.org/v1"; then
  ok "encryption: age snapshot stored encrypted only ($sid.tar.gz.age)"
else
  ko "encryption: age snapshot stored encrypted only ($sid)" "$out"$'\n'"$files"
fi
out=$(enc_cli /keys/gpg-backup "$age_job" verify "$sid")
printf '%s' "$out" | grep -q "^OK $sid" && ok "encryption: verify of the age snapshot" || ko "encryption: verify of the age snapshot" "$out"
in_files "rm -f /files/enc/data.txt" >/dev/null
out=$(enc_cli /keys/gpg-backup "$age_job" restore "$sid" --force)
restored "encryption: restore of the age snapshot with identity_file" "$out" \
  "$(in_files 'cat /files/enc/data.txt' | grep -qx enc-data; echo $?)"
# Without the identity age cannot decrypt: a clear error, nothing restored.
in_files "rm -f /files/enc/data.txt" >/dev/null
out=$(enc_cli /keys/gpg-backup "$(enc_job encage '{"mode":"age","recipient":"'"$age_rcpt"'"}')" restore "$sid" --force)
data=$(in_files 'cat /files/enc/data.txt 2>/dev/null')
if printf '%s' "$out" | grep -q "restore finished with errors" && printf '%s' "$out" | grep -q "identity_file" \
   && ! printf '%s' "$out" | grep -q "Traceback" && [ -z "$data" ]; then
  ok "encryption: restore of the age snapshot without an identity fails clearly"
else
  ko "encryption: restore of the age snapshot without an identity fails clearly" "$out"
fi

echo "== encryption: gpg =="
gpg_job=$(enc_job encgpg '{"mode":"gpg","recipient":"'"$gpg_fpr"'"}')
out=$(enc_cli /keys/gpg-backup "$gpg_job" --now); sid=$(printf '%s' "$out" | sid_of)
files=$(stored "$sid")
packets=$(in_files "GNUPGHOME=/keys/gpg-backup gpg --batch --list-packets $ENC_DIR/$sid.tar.gz.gpg 2>&1")
if printf '%s' "$out" | grep -q "finished: success" && printf '%s' "$files" | grep -qx "$sid.tar.gz.gpg" \
   && ! printf '%s' "$files" | grep -qx "$sid.tar.gz" && printf '%s' "$packets" | grep -q ":pubkey enc packet:"; then
  ok "encryption: gpg snapshot stored encrypted only, with the public key alone ($sid.tar.gz.gpg)"
else
  ko "encryption: gpg snapshot stored encrypted only, with the public key alone ($sid)" \
     "$out"$'\n'"$files"$'\n'"$packets"
fi
out=$(enc_cli /keys/gpg-backup "$gpg_job" verify "$sid")
printf '%s' "$out" | grep -q "^OK $sid" && ok "encryption: verify of the gpg snapshot" || ko "encryption: verify of the gpg snapshot" "$out"
in_files "rm -f /files/enc/data.txt" >/dev/null
out=$(enc_cli /keys/gpg-holder "$gpg_job" restore "$sid" --force)
restored "encryption: restore of the gpg snapshot with the secret keyring" "$out" \
  "$(in_files 'cat /files/enc/data.txt' | grep -qx enc-data; echo $?)"

echo "== encryption: failures store the snapshot UNENCRYPTED, never silently =="
for case in 'age|{"mode":"age","recipient":"age1notarecipient"}' \
            'gpg|{"mode":"gpg","recipient":"nobody@backuphelper.test"}'; do
  mode=${case%%|*}
  out=$(enc_cli /keys/gpg-backup "$(enc_job "encfail$mode" "${case#*|}")" --now); sid=$(printf '%s' "$out" | sid_of)
  files=$(stored "$sid")
  # (the tool's stderr inside the log line may span several lines)
  if printf '%s' "$out" | grep -q "finished: warning" \
     && printf '%s' "$out" | grep -q "encryption ($mode) failed: " \
     && printf '%s' "$out" | grep -q "is stored UNENCRYPTED on every destination" \
     && printf '%s' "$files" | grep -qx "$sid.tar.gz"; then
    ok "encryption: $mode failure stored $sid.tar.gz UNENCRYPTED with an error log and a warning"
  else
    ko "encryption: $mode failure stored the snapshot UNENCRYPTED, not silently ($sid)" "$out"$'\n'"$files"
  fi
done
