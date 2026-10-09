# shellcheck shell=bash
# =============================================================================
# e2e suite "alerts" — sourced by scripts/e2e.sh (helpers, $COMPOSE, ok/ko)
# -----------------------------------------------------------------------------
# Real alerts to real receivers, checked on arrival by scripts/e2e_alerts.py:
#
#   sink           records the HTTP channels: the HMAC-signed webhook, Teams,
#                  Slack, Discord, ntfy and Healthchecks
#   mail-starttls  Mailpit on 587, STARTTLS and login required
#   mail-smtps     Mailpit on 465, implicit TLS (SMTPS) and login required
#
# Both SMTP servers present a certificate of the runtime CA; the engine trusts
# it through SSL_CERT_FILE, as an operator's CA bundle would. Three jobs with
# the levels all / warnings / errors run a success, a warning and an error
# scenario. The warning and the error carry run data - a file name and a path
# full of markup - which must arrive HTML-escaped in the HTML part of the mail,
# verbatim in its text part and in the signed webhook, and nowhere in the chat
# channels. Then: SMTPS without trusting the CA must refuse to send, and a
# receiver that never answers must not hold up the run or the next channel.
# =============================================================================

echo "== alerts: receivers =="
E2E_ALERT_SECRET=$(rand_secret)
E2E_NTFY_TOKEN=$(rand_secret)
E2E_SMTP_USER=alerts
E2E_SMTP_PASSWORD=$(rand_secret)
E2E_SMTP_AUTH="$E2E_SMTP_USER:$E2E_SMTP_PASSWORD"   # Mailpit's login (docker-compose.e2e.yml)
export E2E_ALERT_SECRET E2E_NTFY_TOKEN E2E_SMTP_USER E2E_SMTP_PASSWORD E2E_SMTP_AUTH
start_services sink mail-starttls mail-smtps

# alerts_py <command> [args]: scripts/e2e_alerts.py in the engine image, on the e2e network.
alerts_py(){ $COMPOSE run --rm -T -e E2E_ALERT_SECRET -e E2E_NTFY_TOKEN -e E2E_SMTP_USER \
               --entrypoint python backup - "$@" < scripts/e2e_alerts.py 2>&1; }
# alerts_run <scenario> [docker compose run options]: one --now run of the
# scenario, in a data dir of its own (the suites may share the stack).
alerts_run(){
  local cfg; cfg=$(alerts_py config "$1") || { printf '%s\n' "$cfg"; return 1; }
  shift
  $COMPOSE run --rm -e BACKUP_DATA_DIR=/data/alerts -e BACKUP_CONFIG_JSON="$cfg" \
    -e E2E_ALERT_SECRET -e E2E_NTFY_TOKEN -e E2E_SMTP_USER -e E2E_SMTP_PASSWORD \
    "$@" backup --now 2>&1
}

out=$($COMPOSE run --rm -T --user 0 --entrypoint python backup - seed < scripts/e2e_alerts.py 2>&1) \
  && ok "alerts: sources seeded" || ko "alerts: sources seeded" "$out"

echo "== alerts: success / warning / error at the levels all, warnings, errors =="
for scenario in success warning error; do
  out=$(alerts_run "$scenario" -e SSL_CERT_FILE=/pki/ca.crt)
  runs=$(printf '%s\n' "$out" | grep -c "finished: $scenario")
  failed=$(printf '%s\n' "$out" | grep -E "notification channel '[a-z]+' (failed|skipped)")
  if [ "$runs" = 3 ] && [ -z "$failed" ]; then
    ok "alerts: $scenario run of the three level jobs, every channel delivered"
  else
    ko "alerts: $scenario run of the three level jobs ($runs of 3 finished: $scenario)" "$out"
  fi
done

echo "== alerts: SMTPS to a server the engine cannot verify =="
# No SSL_CERT_FILE: the runtime CA is not trusted, so the mail must not go out
# (implicit_tls verifies the certificate) - and the webhook after it still must.
out=$(alerts_run untrusted)
if printf '%s' "$out" | grep -q "notification channel 'email' failed" \
   && printf '%s' "$out" | grep -q "CERTIFICATE_VERIFY_FAILED" \
   && printf '%s' "$out" | grep -q "finished: success"; then
  ok "alerts: SMTPS refused the untrusted certificate (CERTIFICATE_VERIFY_FAILED), run unaffected"
else
  ko "alerts: SMTPS refused the untrusted certificate" "$out"
fi

echo "== alerts: a receiver that never answers =="
# The webhook goes to a sink path that accepts the request and never answers.
# The channel must fail on its own time limit and Slack after it still deliver;
# the run is killed after 180 s, which counts as hanging.
cfg=$(alerts_py config stall)
started=$SECONDS
# shellcheck disable=SC2086 # $COMPOSE is a command line and must split into words
out=$(timeout 180 $COMPOSE run --rm --name bh-e2e_STALL -e BACKUP_DATA_DIR=/data/alerts \
        -e BACKUP_CONFIG_JSON="$cfg" backup --now 2>&1)
rc=$?
took=$((SECONDS - started))
docker rm -f bh-e2e_STALL >/dev/null 2>&1
if [ "$rc" = 124 ]; then
  ko "alerts: a receiver that never answers held up the run (killed after ${took}s)" "$out"
elif printf '%s' "$out" | grep -q "notification channel 'webhook' failed" \
     && printf '%s' "$out" | grep -q "finished: success"; then
  ok "alerts: a receiver that never answers failed its channel after ${took}s, the run went on"
else
  ko "alerts: a receiver that never answers (exit $rc after ${took}s)" "$out"
fi

echo "== alerts: what arrived =="
out=$(alerts_py check)
tally "$out" $?
