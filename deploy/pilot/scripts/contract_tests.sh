#!/usr/bin/env bash
# Executor contract tests E1, E4, E5, E6 against the running pilot stack
# (platform/executor/README.md "계약 시험"). E2 and E3 (resource_locks) run as DB tests in
# platform/executor/tests/test_resource_locks.py against a real PostgreSQL.
#
#   E4  executor_observe and pgb_stats cannot change roles, fences, subscriptions, data or the pool
#   E5  a command carrying an older epoch is refused by bravo_fence on the resource
#   E1  the Executor dies right after PgBouncer PAUSE returns (fault injection, response lost);
#       the restarted Executor does not repeat it, starts observe-only, the run is SAFE_HOLD
#   E6  the PAUSE effect stays UNKNOWN while an old admin session is still in SHOW CLIENTS
#   then the runbook step a person would take: RESUME, record the resolution, restart normally.
#
# Needs a data plane in its initial state (scripts/reset_data_plane.sh). Settings from .env
# (SMOKE_PROJECT, SMOKE_COMPOSE_FILES, SMOKE_EXECUTOR_URL). Evidence: runs/contract-<ts>/.
set -Eeuo pipefail

PILOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PILOT_DIR"
[ -f .env ] || { echo "[contract] .env missing" >&2; exit 2; }
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  if [ -z "${!key+x}" ]; then export "$key=${line#*=}"; fi
done < .env
: "${SMOKE_COMPOSE_FILES:?}" "${SMOKE_PROJECT:?}" "${SMOKE_EXECUTOR_URL:?}"
read -r -a compose_files <<< "$SMOKE_COMPOSE_FILES"
COMPOSE=(docker compose -p "$SMOKE_PROJECT" --env-file .env)
for f in "${compose_files[@]}"; do COMPOSE+=(-f "$f"); done
EXEC="$SMOKE_EXECUTOR_URL"
OUT="$PILOT_DIR/runs/contract-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"

log()  { echo "[contract $(date +%H:%M:%S)] $*"; }
fail() { echo "[contract $(date +%H:%M:%S)] FAIL: $*" >&2; exit 1; }
pass() { echo "[contract $(date +%H:%M:%S)] PASS $*"; }

# psql inside src-db as a given role; stdin is the SQL. Prints "ok" or the error text.
as_role() {  # as_role <user> <password> <host> <port> <db> <sql>
  "${COMPOSE[@]}" exec -T -e PGPASSWORD="$2" src-db \
    psql -h "$3" -p "$4" -U "$1" -d "$5" -v ON_ERROR_STOP=1 -tA -c "$6" 2>&1 || true
}
expect_denied() {  # expect_denied <label> <output> <pattern>
  echo "$1: $2" >> "$OUT/e4.txt"
  grep -qiE "$3" <<< "$2" || fail "E4 $1 was not refused: $2"
}
platform_sql() {
  "${COMPOSE[@]}" exec -T platform-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA' <<< "$1"
}
recreate_executor() {  # recreate_executor <fault spec or empty>
  BRAVO_FAULT_CRASH_AFTER="$1" "${COMPOSE[@]}" up -d --no-deps --force-recreate executor >/dev/null 2>&1
}
wait_healthy() {
  for _ in $(seq 1 60); do curl -fsS "$EXEC/healthz" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "executor did not become healthy"
}

wait_healthy
curl -fsS "$EXEC/healthz" | jq -e '.observe_only == false' >/dev/null || fail "executor must start in normal mode"
curl -fsS "$EXEC/v1/readiness" | jq -e '.direction == "forward" and .sub_state == "streaming"' >/dev/null \
  || fail "data plane is not in its initial state (run scripts/reset_data_plane.sh first)"

# ---- E4 --------------------------------------------------------------------------------
log "E4: observe-only credentials cannot write"
chat="${CHAT_DB_NAME:?}"
o() { as_role executor_observe "$EXECUTOR_OBSERVE_PASSWORD" src-db 5432 "$chat" "$1"; }
[ "$(o 'SELECT count(*) >= 0 FROM messages')" = "t" ] || fail "E4 executor_observe cannot read"
expect_denied "fence update"    "$(o 'UPDATE bravo_fence SET epoch = epoch WHERE id = 1')" "permission denied"
expect_denied "barrier insert"  "$(o "INSERT INTO bravo_barrier (plan_id, epoch) VALUES ('e4', 0)")" "permission denied"
expect_denied "alter role"      "$(o 'ALTER ROLE app_writer NOLOGIN')" "permission denied"
expect_denied "revoke"          "$(o 'REVOKE INSERT ON messages FROM app_writer')" "permission denied|no privileges"
[ "$(o "SELECT has_table_privilege('app_writer', 'messages', 'INSERT')")" = "t" ] \
  || fail "E4 the REVOKE attempt by executor_observe changed the writer's privilege"
expect_denied "subscription"    "$(o 'ALTER SUBSCRIPTION bravo_rev_sub ENABLE')" "must be owner|permission denied"
expect_denied "pgbouncer PAUSE" "$(as_role pgb_stats "$PGB_STATS_PASSWORD" pgbouncer 6432 pgbouncer 'PAUSE chat')" "admin access|not allowed|permission"
expect_denied "pgbouncer RELOAD" "$(as_role pgb_stats "$PGB_STATS_PASSWORD" pgbouncer 6432 pgbouncer 'RELOAD')" "admin access|not allowed|permission"
pass "E4 (details: $OUT/e4.txt)"

# ---- E1 + E6 ---------------------------------------------------------------------------
log "E6 setup: an old admin session that stays connected to PgBouncer"
"${COMPOSE[@]}" exec -T -e PGPASSWORD="$PGB_ADMIN_PASSWORD" src-db \
  sh -c 'sleep 120 | psql -h pgbouncer -p 6432 -U pgb_admin -d pgbouncer -q >/dev/null 2>&1' &
old_admin_pid=$!
sleep 1

log "E1: restart the Executor with fault injection after pgbouncer/0:PAUSE"
recreate_executor "pgbouncer/0:PAUSE"
wait_healthy
plan_id="$(uuidgen | tr 'A-Z' 'a-z')"
epoch="$(date +%s)"
body="$(jq -nc --arg p "$plan_id" --argjson e "$epoch" '{plan_id:$p, epoch:$e, approval_id:"e1", write_pause_budget_ms:5000}')"
run_id="$(curl -fsS -X POST "$EXEC/v1/cutover" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: e1-$(uuidgen)" -d "$body" | jq -r .run_id)"
cid="$("${COMPOSE[@]}" ps -a -q executor)"
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Running}}' "$cid")" = "false" ] && break
  sleep 1
done
exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$cid")"
[ "$(docker inspect -f '{{.State.Running}}' "$cid")" = "false" ] || fail "executor did not exit after the injected fault"
[ "$exit_code" = "86" ] || fail "executor exit code $exit_code (want 86, fault injection)"
platform_sql "SELECT resource, action, state FROM effects WHERE run_id = '$run_id' ORDER BY observed_at NULLS LAST" \
  | tee "$OUT/e1-effects-after-crash.txt"
grep -q '^pgbouncer/0|PAUSE|IN_FLIGHT$' "$OUT/e1-effects-after-crash.txt" || fail "E1 PAUSE effect not left IN_FLIGHT"
paused="$(as_role pgb_stats "$PGB_STATS_PASSWORD" pgbouncer 6432 pgbouncer 'SHOW DATABASES' | awk -F'|' '$1=="chat"{print $(NF-1)}')"
echo "pool paused after crash: $paused" | tee -a "$OUT/e1.txt"
[ "$paused" = "1" ] || fail "E1 the pool should still be paused by the dead Executor"

log "E1: restart the Executor normally"
recreate_executor ""
wait_healthy
curl -fsS "$EXEC/healthz" | tee "$OUT/e1-healthz.json" | jq -c .
jq -e '.observe_only == true and .observe_reason.in_flight_marked_unknown >= 1' "$OUT/e1-healthz.json" >/dev/null \
  || fail "E1 restarted Executor is not observe-only"
curl -fsS "$EXEC/v1/runs/$run_id" > "$OUT/e1-run.json"
jq -e '.status == "SAFE_HOLD"' "$OUT/e1-run.json" >/dev/null || fail "E1 run is not SAFE_HOLD"
jq -e '[.effects[] | select(.resource == "pgbouncer/0" and .action == "PAUSE")] | length == 1' "$OUT/e1-run.json" \
  >/dev/null || fail "E1 PAUSE was issued more than once"
jq -e '[.effects[] | select(.action == "NOLOGIN")] | length == 0' "$OUT/e1-run.json" >/dev/null \
  || fail "E1 the run continued after the crash"
jq -e '.stages | map(.name) | index("safe_hold_on_restart") != null' "$OUT/e1-run.json" >/dev/null \
  || fail "E1 safe_hold_on_restart stage missing"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$EXEC/v1/cutover" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: e1-new-$(uuidgen)" -d "$(jq -c '.epoch += 1' <<< "$body")")"
[ "$code" = "503" ] || fail "E1 new run in observe-only mode returned $code (want 503)"
curl -fsS "$EXEC/v1/readiness" > "$OUT/e1-readiness-observe.json" || fail "E1 readiness unavailable in observe-only mode"
pass "E1 (no repeat, observe-only, SAFE_HOLD, new runs 503, reads via executor_observe)"

jq -e '[.effects[] | select(.resource == "pgbouncer/0" and .action == "PAUSE")][0]
       | .state == "UNKNOWN" and (.evidence.pgbouncer.admin_clients | length) >= 1' "$OUT/e1-run.json" >/dev/null \
  || fail "E6 PAUSE effect is not UNKNOWN with the old admin session as evidence"
pass "E6 (PAUSE effect UNKNOWN, old admin session listed in SHOW CLIENTS evidence)"

# ---- E5 --------------------------------------------------------------------------------
log "E5: a late command with an older epoch is refused by bravo_fence"
fence_now="$(as_role executor_ctl "$EXECUTOR_CTL_PASSWORD" src-db 5432 "$chat" 'SELECT epoch FROM bravo_fence')"
late="$(as_role executor_ctl "$EXECUTOR_CTL_PASSWORD" src-db 5432 "$chat" \
  "UPDATE bravo_fence SET epoch = $((fence_now - 1)) WHERE id = 1 AND epoch <= $((fence_now - 1)) RETURNING epoch")"
echo "fence=$fence_now late(epoch $((fence_now - 1))) returned: '${late}'" | tee "$OUT/e5.txt"
case "$late" in ''|'UPDATE 0') ;; *) fail "E5 the older epoch updated the fence: $late" ;; esac
pass "E5 (0 rows for epoch $((fence_now - 1)) < $fence_now)"

# ---- runbook: a person resolves the SAFE_HOLD -----------------------------------------
log "runbook: end the old admin session, RESUME the pool, record the resolution"
kill "$old_admin_pid" >/dev/null 2>&1 || true
# The in-container psql of the old session ends when its stdin (sleep 120) closes.
as_role pgb_admin "$PGB_ADMIN_PASSWORD" pgbouncer 6432 pgbouncer 'RESUME chat' >> "$OUT/runbook.txt"
platform_sql "
  UPDATE effects SET state = 'CONFIRMED_APPLIED', observed_at = now(),
         evidence = evidence || jsonb_build_object('resolution', jsonb_build_object(
           'by', 'contract_tests.sh runbook', 'observed', 'pool was paused; RESUME sent by operator'))
   WHERE run_id = '$run_id' AND state IN ('UNKNOWN', 'IN_FLIGHT', 'PREPARED');
  DELETE FROM resource_locks WHERE run_id = '$run_id';" >> "$OUT/runbook.txt"
recreate_executor ""
wait_healthy
curl -fsS "$EXEC/healthz" | jq -e '.observe_only == false' >/dev/null || fail "executor still observe-only after resolution"
pass "runbook: resolved, Executor back in normal mode. Evidence: $OUT"
