#!/usr/bin/env bash
# Executor contract tests on the clusters (platform/executor/README.md 계약 시험; audit B12, B13).
#   E4  executor_observe and pgb_stats cannot write (DB statements and PgBouncer PAUSE/RELOAD)
#   E5  POST /v1/cutover with an epoch below the fence -> the run ends ABORT_PRE_WRITE with the
#       fence effect CONFIRMED_NOT_APPLIED (FenceRejected from the resource), nothing else applied
#   E1  Executor killed right after pgbouncer/src PAUSE returns (test flag) -> restart: no repeat,
#       observe-only, run SAFE_HOLD, new runs 503
#   E6  that PAUSE effect stays UNKNOWN while an old admin session is still in SHOW CLIENTS
#   runbook: RESUME as an operator, read the real SHOW DATABASES row, and record the resolution
#            only if it shows paused = 0 (the real row is stored as evidence)
# E2, E3 are DB tests (platform/executor/tests/test_resource_locks.py).
# Executor: executor-0 on the platform EKS (port-forward from lib.sh); E1 sets and clears its two
# test-only env vars. Needs: link up, the initial (pre-cutover) state, and APPROVAL_E5 / APPROVAL_E1 =
# two cutover approvals the platform API recorded for PLAN_ID (approvals are never invented here).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
executor_url; URL="$EXECUTOR_URL"
: "${PLAN_ID:?PLAN_ID of the approved plan}" "${APPROVAL_E5:?cutover approval id from the platform API}" \
  "${APPROVAL_E1:?second cutover approval id from the platform API}"
OUT="$RUNS/cloud-contract-$(date +%Y%m%dT%H%M%S)"; mkdir -p "$OUT"
pass() { log "PASS $*"; }

curl -fsS "$URL/healthz" | jq -e '.observe_only == false' >/dev/null || fail "executor must be in normal mode"

# ---- E4 ---------------------------------------------------------------------------------
for side in src dst; do ops db "$side" e4 > "$OUT/e4-$side.json"; jq -c . "$OUT/e4-$side.json"; done
jq -e -s 'all(.[]; .all_refused == true)' "$OUT"/e4-*.json >/dev/null || fail "E4: a write was not refused"
pass "E4"

# ---- E5 through the Executor --------------------------------------------------------------
fence="$(ops db src fence | jq -r .epoch)"
low=$((fence - 1))
body="$(jq -nc --arg p "$PLAN_ID" --arg a "$APPROVAL_E5" --argjson e "$low" \
  '{plan_id:$p, epoch:$e, approval_id:$a, write_pause_budget_ms:5000}')"
code="$(curl -sS -o "$OUT/e5-accepted.json" -w '%{http_code}' -X POST "$URL/v1/cutover" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: e5-$(uuidgen)" -d "$body")"
[ "$code" = "202" ] || fail "E5: POST returned $code $(cat "$OUT/e5-accepted.json")"
id="$(jq -r .run_id "$OUT/e5-accepted.json")"
while [ "$(curl -fsS "$URL/v1/runs/$id" | jq -r .status)" = "RUNNING" ]; do sleep 1; done
curl -fsS "$URL/v1/runs/$id" > "$OUT/e5-run.json"
jq -c '{status, stages: [.stages[] | {name, result, reason: .detail.reason}], effects: [.effects[] | "\(.resource) \(.action) \(.state)"]}' "$OUT/e5-run.json"
jq -e --argjson low "$low" '.status == "ABORT_PRE_WRITE"
  and ([.effects[] | select(.action == "FENCE_EPOCH" and .state == "CONFIRMED_NOT_APPLIED"
        and .evidence.reason == "FENCE_REJECTED")] | length) >= 1
  and ([.effects[] | select(.state == "CONFIRMED_APPLIED")] | length) == 0' "$OUT/e5-run.json" >/dev/null \
  || fail "E5: the lower epoch was not refused by the resource fence"
pass "E5 (epoch $low < fence $fence refused by bravo_fence)"

# ---- E1 + E6 --------------------------------------------------------------------------------
log "E6 setup: an operator admin session that stays connected to the source PgBouncer"
ops pool src hold > "$OUT/e6-held-session.json" 2>&1 &
sleep 3
before="$(executor_restarts)"
executor_set_fault "pgbouncer/src:PAUSE"
body="$(jq -nc --arg p "$PLAN_ID" --arg a "$APPROVAL_E1" --argjson e "$(date +%s)" \
  '{plan_id:$p, epoch:$e, approval_id:$a, write_pause_budget_ms:5000}')"
id="$(curl -fsS -X POST "$URL/v1/cutover" -H 'Content-Type: application/json' -H "Idempotency-Key: e1-$(uuidgen)" -d "$body" | jq -r .run_id)"
for _ in $(seq 1 90); do [ "$(executor_restarts)" -gt "$before" ] && break; sleep 2; done
[ "$(executor_restarts)" -gt "$before" ] || fail "E1: executor-0 did not exit at the injected point"
EXECUTOR_URL=""; kill "$EXECUTOR_PF_PID" 2>/dev/null || true
ops pool src show > "$OUT/e1-pool-after-crash.json"; jq -c . "$OUT/e1-pool-after-crash.json"
jq -e '.row.paused == 1' "$OUT/e1-pool-after-crash.json" >/dev/null || fail "E1: the pool should still be paused"
executor_clear_fault; URL="$EXECUTOR_URL"
curl -fsS "$URL/healthz" > "$OUT/e1-healthz.json"
jq -e '.observe_only == true' "$OUT/e1-healthz.json" >/dev/null || fail "E1: not observe-only after restart"
curl -fsS "$URL/v1/runs/$id" > "$OUT/e1-run.json"
jq -e '.status == "SAFE_HOLD" and ([.effects[] | select(.resource == "pgbouncer/src" and .action == "PAUSE")] | length) == 1' \
  "$OUT/e1-run.json" >/dev/null || fail "E1: not SAFE_HOLD or PAUSE repeated"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL/v1/cutover" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: e1-new-$(uuidgen)" -d "$body")"
[ "$code" = "503" ] || fail "E1: new run in observe-only returned $code"
pass "E1"
jq -e '[.effects[] | select(.resource == "pgbouncer/src" and .action == "PAUSE")][0] | .state == "UNKNOWN"
  and (.evidence.pgbouncer.src.admin_clients | length) >= 1' "$OUT/e1-run.json" >/dev/null \
  || fail "E6: PAUSE effect not UNKNOWN with the old admin session as evidence"
pass "E6"

# ---- runbook (B13): act, read the real state, record only what was observed -----------------------
ops pool src resume > "$OUT/runbook-resume.json"; jq -c . "$OUT/runbook-resume.json"
jq -e '.row.paused == 0' "$OUT/runbook-resume.json" >/dev/null || fail "runbook: pool still paused; nothing recorded"
log "runbook: the pool shows paused = 0; resolution must be recorded through the platform (person + evidence $OUT/runbook-resume.json)"
log "evidence: $OUT"
