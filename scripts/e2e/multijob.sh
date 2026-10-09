# shellcheck shell=bash
# =============================================================================
# e2e suite "multijob" — sourced by scripts/e2e.sh (helpers, $COMPOSE, ok/ko)
# -----------------------------------------------------------------------------
# Two jobs in one data dir under the scheduler daemon, as a stack runs them:
# alpha (retention count 1) and beta (count 3) share a cron schedule, so the
# scheduler starts both in the same second. Checked: job-scoped snapshot ids
# that do not collide, every job pruning only its own snapshots, prune --job,
# a job-scoped id restoring into the job it names, a clean SIGTERM drain, and
# the healthcheck judging every job on its own - a failed job, and a job's own
# healthcheck_max_age_hours.
# =============================================================================

echo "== multijob: two jobs, one data dir, one daemon =="
MJ_DIR=/data/multijob   # a data dir of its own (the suites may share the stack)
out=$($COMPOSE run --rm --user 0 --entrypoint sh backup -c \
  'rm -rf /files/multijob && mkdir -p /files/multijob/alpha /files/multijob/beta &&
   echo alpha-data > /files/multijob/alpha/data.txt && echo beta-data > /files/multijob/beta/data.txt &&
   chown -R 1000:1000 /files/multijob' 2>&1) \
  && ok "multijob: sources seeded" || ko "multijob: sources seeded" "$out"

mj_job(){ printf '{"name":"%s","sources":[{"type":"filesystem","name":"%s","path":"/files/multijob/%s"}],"schedule":{"cron":"* * * * *","on_startup":true},"retention":{"count":%s}}' "$1" "$1" "$1" "$2"; }
mj='{"instance_name":"e2e-multijob","jobs":['"$(mj_job alpha 1)"','"$(mj_job beta 3)"']}'
# mj_cli <config> <command...>: one engine command in the suite's data dir.
mj_cli(){ local cfg=$1; shift
          $COMPOSE run --rm -e BACKUP_DATA_DIR="$MJ_DIR" -e BACKUP_CONFIG_JSON="$cfg" backup "$@" 2>&1; }
# mj_ids [job]: the snapshot ids in the data dir (of one job), oldest first.
mj_ids(){ in_files "ls $MJ_DIR 2>/dev/null" | sed -n 's/\.manifest\.json$//p' | grep -E "_${1:-[a-z]+}\$" | sort; }

out=$($COMPOSE run -d --name bh-e2e_DAEMON -e BACKUP_DATA_DIR="$MJ_DIR" -e BACKUP_CONFIG_JSON="$mj" backup 2>&1) \
  || ko "multijob: daemon started" "$out"
# Round 1 runs at the start (on_startup), round 2 at the next minute boundary,
# where the cron trigger fires both jobs together. Done when beta (count 3)
# holds two snapshots and alpha's only one (count 1) is from round 2 as well.
for _ in $(seq 1 30); do
  sleep 5
  alpha_ids=$(mj_ids alpha); beta_ids=$(mj_ids beta)
  alpha_last=$(printf '%s\n' "$alpha_ids" | tail -n 1)
  beta_last=$(printf '%s\n' "$beta_ids" | tail -n 1)
  n_alpha=$(printf '%s' "$alpha_ids" | grep -c .); n_beta=$(printf '%s' "$beta_ids" | grep -c .)
  if [ "$n_beta" -ge 2 ] && [ -n "$alpha_last" ] && [ "${alpha_last%_alpha}" = "${beta_last%_beta}" ]; then
    break
  fi
done
echo "  alpha: $(printf '%s' "$alpha_ids" | tr '\n' ' ')"
echo "  beta:  $(printf '%s' "$beta_ids" | tr '\n' ' ')"
round2="${beta_last%_beta}"
if [ -n "$round2" ] && [ "$alpha_last" = "${round2}_alpha" ]; then
  ok "multijob: both jobs started in the same second ($round2), ids ${round2}_alpha / ${round2}_beta"
else
  ko "multijob: both jobs started in the same second" "$(docker logs bh-e2e_DAEMON 2>&1)"
fi
[ "$n_alpha" = 1 ] && [ "$n_beta" = 2 ] \
  && ok "multijob: alpha kept 1 snapshot (count 1), beta 2 (count 3) - each pruned only its own" \
  || ko "multijob: per-job retention: alpha $n_alpha (want 1), beta $n_beta (want 2)"
others=$(in_files "ls $MJ_DIR" | grep '\.manifest\.json$' | grep -vE '_(alpha|beta)\.manifest\.json$')
[ -z "$others" ] && ok "multijob: every snapshot id names its job" || ko "multijob: ids without a job" "$others"
st=$(in_files "cat $MJ_DIR/.state/job-alpha.json $MJ_DIR/.state/job-beta.json" | grep -c '"status": "success"')
[ "$st" = 2 ] && ok "multijob: a run record per job, both success" || ko "multijob: run records" "$st"

started=$SECONDS
docker stop -t 60 bh-e2e_DAEMON >/dev/null 2>&1
code=$(docker inspect -f '{{.State.ExitCode}}' bh-e2e_DAEMON 2>&1)
logs=$(docker logs bh-e2e_DAEMON 2>&1)
docker rm -f bh-e2e_DAEMON >/dev/null 2>&1
if [ "$code" = 0 ] && printf '%s' "$logs" | grep -q "draining the running job"; then
  ok "multijob: SIGTERM drained the daemon, exit 0 after $((SECONDS - started))s"
else
  ko "multijob: SIGTERM drain (exit code $code)" "$logs"
fi

for sid in "$alpha_last" "$beta_last"; do
  out=$(mj_cli "$mj" verify "$sid")
  printf '%s' "$out" | grep -q "^OK $sid" && ok "multijob: verify $sid" || ko "multijob: verify $sid" "$out"
done

# A job-scoped id restores into the job it names, without --job.
in_files "rm /files/multijob/beta/data.txt" >/dev/null
out=$(mj_cli "$mj" restore "$beta_last" --force)
restored "multijob: restore of $beta_last into job beta" "$out" \
  "$(in_files 'cat /files/multijob/beta/data.txt' | grep -qx beta-data; echo $?)"

out=$(mj_cli "$mj" prune --job beta --keep 1)
a=$(mj_ids alpha | wc -l); b=$(mj_ids beta | wc -l)
[ "$a" = 1 ] && [ "$b" = 1 ] \
  && ok "multijob: prune --job beta --keep 1 pruned beta only (alpha $a, beta $b)" \
  || ko "multijob: prune --job beta: alpha $a (want 1), beta $b (want 1)" "$out"

echo "== multijob: the healthcheck judges every job on its own =="
out=$(mj_cli "$mj" healthcheck); rc=$?
if [ "$rc" = 0 ] && printf '%s' "$out" | grep -q "job alpha: the last backup is fresh" \
   && printf '%s' "$out" | grep -q "job beta: the last backup is fresh"; then
  ok "multijob: healthy, both jobs fresh"
else
  ko "multijob: healthy, both jobs fresh (exit $rc)" "$out"
fi
# alpha's own limit: 0.0005 h (1.8 s); beta keeps the default 26 h.
mj_age=$(printf '%s' "$mj" | sed 's/"name":"alpha",/"name":"alpha","healthcheck_max_age_hours":0.0005,/')
sleep 3
out=$(mj_cli "$mj_age" healthcheck); rc=$?
if [ "$rc" = 1 ] && printf '%s' "$out" | grep -q "job alpha: the last backup is stale" \
   && ! printf '%s' "$out" | grep -q "job beta"; then
  ok "multijob: alpha stale by its own healthcheck_max_age_hours, beta not reported"
else
  ko "multijob: alpha stale by its own max age (exit $rc)" "$out"
fi
# beta fails, alpha succeeds after it: still unhealthy, naming beta only.
in_files "rm -rf /files/multijob/beta" >/dev/null
out=$(mj_cli "$mj" --now)
printf '%s' "$out" | grep -q "job alpha snapshot .* finished: success" \
  && printf '%s' "$out" | grep -q "job beta snapshot .* finished: error" \
  && ok "multijob: --now ran alpha (success) and beta (error, path gone)" \
  || ko "multijob: --now with beta's path gone" "$out"
out=$(mj_cli "$mj" healthcheck); rc=$?
if [ "$rc" = 1 ] && printf '%s' "$out" | grep -q "job beta: the last backup failed" \
   && ! printf '%s' "$out" | grep -q "job alpha"; then
  ok "multijob: unhealthy for beta's failure although alpha ran later and succeeded"
else
  ko "multijob: beta's failure not masked by alpha (exit $rc)" "$out"
fi
