#!/usr/bin/env bash
# Pilot integration scenario for the data plane and the Executor (CONTRACT.md 3-6).
#
#   1. bring up src-db, dst-db, repl-setup, pgbouncer, executor (+ platform-db, migrate)
#   2. controls: dst DB-ANON-READ must FAIL (planted regression) -> fix-dst-grant.sql -> PASS
#   3. start loadgen (writes through PgBouncer, idempotent retries)
#   4. POST /v1/cutover twice with the same Idempotency-Key -> same run_id
#   5. wait for the run; assert DONE, receipt verdict MATCH, write_pause_ms recorded
#   6. wait for loadgen; assert NFR-2 (every acked write present exactly once, no failed request)
#
# SMOKE_SCENARIO=match (default) expects DONE + MATCH. SMOKE_SCENARIO=mismatch writes one stray
# row straight into dst (superuser, bypassing PgBouncer) before the cutover and expects
# ABORT_PRE_WRITE + MISMATCH with the documented restoration (pool resumed on src, NFR-2 still holds).
# SMOKE_SCENARIO=roundtrip runs the cutover, keeps the load on dst for SMOKE_FAILBACK_AFTER_S, then
# POST /v1/failback and expects DONE + MATCH (direction=failback); every message written on dst
# must be on src afterwards (S4-AC1) and NFR-2 must hold on the final DB (src).
#
# SMOKE_DEMO_WS_URLS="ws://localhost:8090/ws ws://localhost:8091/ws" adds chat traffic through the
# demo app instances (scripts/demo_traffic.py, needs uv) next to loadgen, verified the same way.
# The roundtrip also writes attachment rows through PgBouncer (no objects: object stores are not
# configured here) to exercise the D-04 freeze, store switch and the v1.1 uuid-PK receipt hash.
#
# Settings come from deploy/pilot/.env (SMOKE_* entries, see .env.example).
# SMOKE_FRESH=1 removes the project's containers and volumes first (destroys pilot data).
# Evidence is written to deploy/pilot/runs/smoke-<timestamp>/ (gitignored).
set -Eeuo pipefail

PILOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PILOT_DIR"

if [ ! -f .env ]; then
  echo "[smoke] .env missing: cp .env.example .env and replace every change-me value" >&2
  exit 2
fi
# Load .env without overriding variables already set in the caller's environment.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  if [ -z "${!key+x}" ]; then export "$key=${line#*=}"; fi
done < .env

: "${SMOKE_COMPOSE_FILES:?set SMOKE_COMPOSE_FILES}" "${SMOKE_PROJECT:?set SMOKE_PROJECT}"
: "${SMOKE_EXECUTOR_URL:?set SMOKE_EXECUTOR_URL}" "${SMOKE_LOAD_DURATION_S:?set SMOKE_LOAD_DURATION_S}"
: "${SMOKE_CUTOVER_AFTER_S:?set SMOKE_CUTOVER_AFTER_S}" "${SMOKE_WRITE_PAUSE_BUDGET_MS:?set SMOKE_WRITE_PAUSE_BUDGET_MS}"
SMOKE_FRESH="${SMOKE_FRESH:-0}"
SMOKE_SCENARIO="${SMOKE_SCENARIO:-match}"
SMOKE_FAILBACK_AFTER_S="${SMOKE_FAILBACK_AFTER_S:-15}"
case "$SMOKE_SCENARIO" in match|mismatch|roundtrip) ;;
  *) echo "[smoke] SMOKE_SCENARIO must be match, mismatch or roundtrip" >&2; exit 2 ;; esac
SMOKE_RUN_TIMEOUT_S="${SMOKE_RUN_TIMEOUT_S:-120}"

read -r -a compose_files <<< "$SMOKE_COMPOSE_FILES"
COMPOSE=(docker compose -p "$SMOKE_PROJECT" --env-file .env)
for f in "${compose_files[@]}"; do COMPOSE+=(-f "$f"); done
EXEC="$SMOKE_EXECUTOR_URL"
EVIDENCE_DIR="$PILOT_DIR/runs/smoke-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$EVIDENCE_DIR"

log()  { echo "[smoke $(date +%H:%M:%S)] $*"; }
fail() { echo "[smoke $(date +%H:%M:%S)] FAIL: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "missing tool: $1"; }
need docker; need curl; need jq; need uuidgen

wait_for() {  # wait_for <timeout_s> <description> <command...>
  local timeout="$1" what="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "timeout waiting for $what"
    sleep 1
  done
}

if [ "$SMOKE_FRESH" = "1" ]; then
  log "SMOKE_FRESH=1: removing project ${SMOKE_PROJECT} containers and volumes"
  "${COMPOSE[@]}" --profile load down -v --remove-orphans
fi

log "building images and starting the data plane + executor"
"${COMPOSE[@]}" --profile load build executor loadgen pgbouncer
"${COMPOSE[@]}" up -d executor
wait_for 180 "executor /healthz" curl -fsS "$EXEC/healthz"
curl -fsS "$EXEC/healthz" | jq -e '.observe_only == false' >/dev/null \
  || fail "executor is in observe-only mode (unresolved effects from an earlier run)"

log "waiting for forward replication to stream"
wait_for 120 "sub_state=streaming" sh -c "curl -fsS '$EXEC/v1/readiness' | jq -e '.sub_state == \"streaming\"'"
curl -fsS "$EXEC/v1/readiness" | tee "$EVIDENCE_DIR/readiness-before.json" | jq -c .

log "controls before the fix"
curl -fsS "$EXEC/v1/controls" | tee "$EVIDENCE_DIR/controls-before.json" | jq -c .
anon_dst="$(jq -r '.[] | select(.control_id=="DB-ANON-READ") | .dst' "$EVIDENCE_DIR/controls-before.json")"
if [ "$anon_dst" = "FAIL" ]; then
  log "dst DB-ANON-READ=FAIL (planted regression) -> applying fix-dst-grant.sql"
  "${COMPOSE[@]}" exec -T dst-db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U postgres -d "$POSTGRES_DB"' \
    < fix-dst-grant.sql
fi
curl -fsS "$EXEC/v1/controls" | tee "$EVIDENCE_DIR/controls-after.json" | jq -c .
jq -e 'all(.[]; .src == "PASS" and .dst == "PASS")' "$EVIDENCE_DIR/controls-after.json" >/dev/null \
  || fail "controls are not all PASS after the fix"

log "starting loadgen for ${SMOKE_LOAD_DURATION_S}s at ${LOADGEN_RATE}/s"
export LOADGEN_DURATION_S="$SMOKE_LOAD_DURATION_S"
"${COMPOSE[@]}" --profile load up -d --force-recreate --no-deps loadgen
loadgen_cid="$("${COMPOSE[@]}" --profile load ps -a -q loadgen)"
[ -n "$loadgen_cid" ] || fail "loadgen container not found"
demo_pid=""
if [ -n "${SMOKE_DEMO_WS_URLS:-}" ]; then
  need uv
  demo_args=()
  for u in $SMOKE_DEMO_WS_URLS; do demo_args+=(--url "$u"); done
  log "starting demo app traffic via ${SMOKE_DEMO_WS_URLS}"
  uv run --quiet --project "$PILOT_DIR/../../apps/demo" python "$PILOT_DIR/scripts/demo_traffic.py" \
    "${demo_args[@]}" --rate "${SMOKE_DEMO_RATE:-5}" --duration "$SMOKE_LOAD_DURATION_S" \
    --verify-dsn "host=127.0.0.1 port=${PGBOUNCER_HOST_PORT} dbname=${CHAT_DB_NAME} user=app_writer password=${APP_WRITER_PASSWORD}" \
    > "$EVIDENCE_DIR/demo-traffic.json" 2> "$EVIDENCE_DIR/demo-traffic.stderr" &
  demo_pid=$!
fi

# Attachment rows through the app path (PgBouncer, app_writer). D-04 guard: store must be active.
att_insert() {  # att_insert <store_id> -> prints "ok" or the error
  local sha; sha="$(uuidgen | shasum -a 256 | cut -d' ' -f1)"
  "${COMPOSE[@]}" exec -T -e PGPASSWORD="$APP_WRITER_PASSWORD" src-db psql -h pgbouncer -p 6432 -U app_writer \
    -d "$CHAT_DB_NAME" -v ON_ERROR_STOP=1 -tA -c "INSERT INTO attachments (logical_id, idem_key, room, author,
      object_key, sha256, size_bytes, content_type, filename, store_id) VALUES (gen_random_uuid(), gen_random_uuid(),
      'smoke', 'smoke', 'att/$sha', '$sha', 1, 'text/plain', 'smoke.txt', '$1')" 2>&1 | tr '\n' ' ' || true
}
sleep "$SMOKE_CUTOVER_AFTER_S"

if [ "$SMOKE_SCENARIO" = "mismatch" ]; then
  log "scenario mismatch: writing one stray row straight into dst (bypass write)"
  "${COMPOSE[@]}" exec -T dst-db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U postgres -d "$POSTGRES_DB" -c \
    "INSERT INTO messages (id, room, author, body, idem_key) VALUES (900000001, '"'"'smoke'"'"', '"'"'bypass'"'"', '"'"'stray row on dst'"'"', gen_random_uuid())"'
fi

if [ "$SMOKE_SCENARIO" = "roundtrip" ]; then
  for _ in 1 2 3; do r="$(att_insert "$ATTACHMENT_SRC_STORE_ID")"; [[ "$r" == INSERT* ]] || fail "attachment row on src: $r"; done
  log "3 attachment rows written on store $ATTACHMENT_SRC_STORE_ID before the cutover"
fi

plan_id="$(uuidgen | tr 'A-Z' 'a-z')"
idem_key="smoke-$(uuidgen | tr 'A-Z' 'a-z')"
epoch="$(date +%s)"
body="$(jq -nc --arg p "$plan_id" --argjson e "$epoch" --argjson b "$SMOKE_WRITE_PAUSE_BUDGET_MS" \
  '{plan_id:$p, epoch:$e, approval_id:"smoke-approval", write_pause_budget_ms:$b}')"

log "POST /v1/cutover (plan=$plan_id epoch=$epoch)"
code="$(curl -sS -o "$EVIDENCE_DIR/cutover-1.json" -w '%{http_code}' -X POST "$EXEC/v1/cutover" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $idem_key" -d "$body")"
[ "$code" = "202" ] || fail "first POST returned $code: $(cat "$EVIDENCE_DIR/cutover-1.json")"
run_id="$(jq -r .run_id "$EVIDENCE_DIR/cutover-1.json")"

code="$(curl -sS -o "$EVIDENCE_DIR/cutover-2.json" -w '%{http_code}' -X POST "$EXEC/v1/cutover" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $idem_key" -d "$body")"
[ "$code" = "202" ] || fail "replayed POST returned $code"
[ "$(jq -r .run_id "$EVIDENCE_DIR/cutover-2.json")" = "$run_id" ] || fail "same Idempotency-Key returned a different run_id"
log "idempotency: same key -> same run_id $run_id"

other_body="$(jq -c '.epoch += 1' <<< "$body")"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$EXEC/v1/cutover" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $idem_key" -d "$other_body")"
[ "$code" = "409" ] || fail "same key with different input returned $code (want 409)"
log "idempotency: same key, different input -> 409"

wait_run() {  # wait_run <run_id> <out.json>
  local id="$1" out="$2" st deadline=$(( $(date +%s) + SMOKE_RUN_TIMEOUT_S ))
  while :; do
    curl -fsS "$EXEC/v1/runs/$id" > "$out"
    st="$(jq -r .status "$out")"
    [ "$st" != "RUNNING" ] && break
    [ "$(date +%s)" -lt "$deadline" ] || fail "run $id still RUNNING after ${SMOKE_RUN_TIMEOUT_S}s"
    sleep 0.5
  done
  jq -c '{kind, status, write_pause_ms, stages: [.stages[] | {name, ms, result}], verdict: .receipt.verdict}' "$out"
}

log "waiting for run $run_id"
wait_run "$run_id" "$EVIDENCE_DIR/run.json"
status="$(jq -r .status "$EVIDENCE_DIR/run.json")"

fb_status=""
if [ "$SMOKE_SCENARIO" = "roundtrip" ]; then
  [ "$status" = "DONE" ] || fail "cutover status $status before failback"
  curl -fsS "$EXEC/v1/readiness" > "$EVIDENCE_DIR/readiness-after-cutover.json"
  jq -c . "$EVIDENCE_DIR/readiness-after-cutover.json"
  r="$(att_insert "$ATTACHMENT_SRC_STORE_ID")"
  [[ "$r" == *"is not the active store"* ]] || fail "D-04: an attachment for the old store was not rejected after cutover: $r"
  for _ in 1 2 3; do r="$(att_insert "$ATTACHMENT_DST_STORE_ID")"; [[ "$r" == INSERT* ]] || fail "attachment row on dst: $r"; done
  log "D-04 after cutover: old store rejected (ZB002), 3 attachment rows written on store $ATTACHMENT_DST_STORE_ID"
  log "load continues on dst for ${SMOKE_FAILBACK_AFTER_S}s"
  sleep "$SMOKE_FAILBACK_AFTER_S"
  fb_key="smoke-fb-$(uuidgen | tr 'A-Z' 'a-z')"
  fb_body="$(jq -c '.epoch += 1 | .approval_id = "smoke-failback-approval"' <<< "$body")"
  log "POST /v1/failback (epoch=$(jq .epoch <<< "$fb_body"))"
  code="$(curl -sS -o "$EVIDENCE_DIR/failback-1.json" -w '%{http_code}' -X POST "$EXEC/v1/failback" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $fb_key" -d "$fb_body")"
  [ "$code" = "202" ] || fail "failback POST returned $code: $(cat "$EVIDENCE_DIR/failback-1.json")"
  fb_run_id="$(jq -r .run_id "$EVIDENCE_DIR/failback-1.json")"
  code="$(curl -sS -o "$EVIDENCE_DIR/failback-2.json" -w '%{http_code}' -X POST "$EXEC/v1/failback" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $fb_key" -d "$fb_body")"
  [ "$(jq -r .run_id "$EVIDENCE_DIR/failback-2.json")" = "$fb_run_id" ] || fail "failback replay returned another run_id"
  log "waiting for failback run $fb_run_id"
  wait_run "$fb_run_id" "$EVIDENCE_DIR/failback-run.json"
  fb_status="$(jq -r .status "$EVIDENCE_DIR/failback-run.json")"
fi

if [ -n "$demo_pid" ]; then
  log "waiting for demo app traffic to finish"
  demo_exit=0; wait "$demo_pid" || demo_exit=$?
  jq -c . "$EVIDENCE_DIR/demo-traffic.json" || true
fi
log "waiting for loadgen to finish"
loadgen_exit="$(docker wait "$loadgen_cid")"
docker logs "$loadgen_cid" > "$EVIDENCE_DIR/loadgen.stdout" 2> "$EVIDENCE_DIR/loadgen.stderr"
tail -n 1 "$EVIDENCE_DIR/loadgen.stdout" > "$EVIDENCE_DIR/loadgen-summary.json"
jq -c . "$EVIDENCE_DIR/loadgen-summary.json"
curl -fsS "$EXEC/v1/readiness" > "$EVIDENCE_DIR/readiness-after.json"

# ---- assertions -----------------------------------------------------------------------
if [ "$SMOKE_SCENARIO" = "match" ] || [ "$SMOKE_SCENARIO" = "roundtrip" ]; then
  [ "$status" = "DONE" ] || fail "run status $status (see $EVIDENCE_DIR/run.json)"
  jq -e '.receipt.verdict == "MATCH"' "$EVIDENCE_DIR/run.json" >/dev/null || fail "receipt verdict is not MATCH"
  jq -e '.write_pause_ms | type == "number"' "$EVIDENCE_DIR/run.json" >/dev/null || fail "write_pause_ms not recorded"
else
  [ "$status" = "ABORT_PRE_WRITE" ] || fail "run status $status, want ABORT_PRE_WRITE (see $EVIDENCE_DIR/run.json)"
  jq -e '.receipt.verdict == "MISMATCH"' "$EVIDENCE_DIR/run.json" >/dev/null || fail "receipt verdict is not MISMATCH"
  jq -e '[.stages[] | select(.name == "abort_restore" and .result == "OK")] | length == 1' "$EVIDENCE_DIR/run.json" \
    >/dev/null || fail "abort_restore stage missing or failed"
  jq -e '.sub_state == "streaming"' "$EVIDENCE_DIR/readiness-after.json" >/dev/null \
    || fail "forward replication not streaming after ABORT_PRE_WRITE"
fi
jq -e '[.effects[] | select(.state != "CONFIRMED_APPLIED")] | length == 0' "$EVIDENCE_DIR/run.json" >/dev/null \
  || fail "some effects are not CONFIRMED_APPLIED"
if [ "$SMOKE_SCENARIO" = "roundtrip" ]; then
  fb="$EVIDENCE_DIR/failback-run.json"
  [ "$fb_status" = "DONE" ] || fail "failback status $fb_status (see $fb)"
  jq -e '.receipt.verdict == "MATCH" and .receipt.direction == "failback"' "$fb" >/dev/null \
    || fail "failback receipt is not MATCH/failback"
  jq -e '.write_pause_ms | type == "number"' "$fb" >/dev/null || fail "failback write_pause_ms not recorded"
  jq -e '[.effects[] | select(.state != "CONFIRMED_APPLIED")] | length == 0' "$fb" >/dev/null \
    || fail "some failback effects are not CONFIRMED_APPLIED"
  # S4-AC1: every message written on dst after the cutover is on src after the failback.
  cut_last="$(jq '.receipt.sequences[] | select(.name == "messages_id_seq") | .origin_last' "$EVIDENCE_DIR/run.json")"
  fb_last="$(jq '.receipt.sequences[] | select(.name == "messages_id_seq") | .origin_last' "$fb")"
  # Rows written while dst was the write DB: (cutover src last, failback dst last]. Fingerprint = count + key hash sum.
  q="SELECT count(*), coalesce(sum(hashtext(idem_key::text)::bigint), 0) FROM messages WHERE id > ${cut_last} AND id <= ${fb_last}"
  on_dst="$("${COMPOSE[@]}" exec -T dst-db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -tA -U postgres -d "$POSTGRES_DB"' <<< "$q")"
  on_src="$("${COMPOSE[@]}" exec -T src-db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -tA -U postgres -d "$POSTGRES_DB"' <<< "$q")"
  echo "{\"written_after_cutover_on_dst\": \"$on_dst\", \"same_rows_on_src\": \"$on_src\"}" > "$EVIDENCE_DIR/s4-ac1.json"
  [ "${on_dst%%|*}" -gt 0 ] || fail "no messages were written on dst between cutover and failback"
  [ "$on_dst" = "$on_src" ] || fail "S4-AC1: dst rows after cutover ($on_dst) differ on src ($on_src)"
  log "S4-AC1: ${on_dst%%|*} messages written on dst after cutover are all on src"
  jq -e '.receipt.hash_rule == "v1.1" and ([.receipt.tables[] | select(.name == "attachments")][0].match == true)' "$fb" \
    >/dev/null || fail "failback receipt does not cover attachments with hash rule v1.1"
fi
[ "$loadgen_exit" = "0" ] || fail "loadgen exited $loadgen_exit"
if [ -n "$demo_pid" ]; then
  [ "$demo_exit" = "0" ] || fail "demo app traffic exited $demo_exit (see demo-traffic.json)"
  jq -e '.nfr2.ok == true and .unacked == 0' "$EVIDENCE_DIR/demo-traffic.json" >/dev/null || fail "demo traffic NFR-2 violated"
fi
jq -e '.nfr2.ok == true and .nfr2.lost == 0 and .nfr2.duplicated == 0 and .nfr2.failed_requests == 0' \
  "$EVIDENCE_DIR/loadgen-summary.json" >/dev/null || fail "NFR-2 violated (see loadgen-summary.json)"

log "PASS  scenario=$SMOKE_SCENARIO run=$run_id status=$status verdict=$(jq -r .receipt.verdict "$EVIDENCE_DIR/run.json") write_pause_ms=$(jq .write_pause_ms "$EVIDENCE_DIR/run.json")"
if [ "$SMOKE_SCENARIO" = "roundtrip" ]; then
  log "      failback run=$fb_run_id status=$fb_status verdict=$(jq -r .receipt.verdict "$EVIDENCE_DIR/failback-run.json") write_pause_ms=$(jq .write_pause_ms "$EVIDENCE_DIR/failback-run.json")"
fi
log "      acked=$(jq .summary.acked "$EVIDENCE_DIR/loadgen-summary.json") lost=0 duplicated=0 failed=0"
log "      evidence: $EVIDENCE_DIR"
