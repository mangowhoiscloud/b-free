#!/usr/bin/env bash
# Data-plane prerequisites for cloud cutover runs, in order, each checked against the real state.
#   bootstrap.sh check   print every prerequisite as ok / 연결 안 됨 + reason (JSON lines), change nothing
#   bootstrap.sh up      DB roles (both envs) -> reverse relay -> link -> replication -> executor-0
#                        health. Stops at the first missing prerequisite and says which.
# Schema: applied only where the database is still empty (db_roles.py --schema); an environment
# owner who already applied apps/demo/schema keeps their schema and only the Executor roles' passwords are set.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
OUT="$RUNS/cloud-bootstrap-$(date +%Y%m%dT%H%M%S).jsonl"
note() { jq -nc --arg item "$1" --arg state "$2" --arg reason "${3:-}" '{item: $item, state: $state, reason: $reason}' | tee -a "$OUT"; }
k() { kubectl --context "$1" -n "$CHAT_NAMESPACE" "${@:2}"; }

check() {
  local ok=0
  ctx_ok "$SRC_CONTEXT" && note "context $SRC_CONTEXT" ok || { note "context $SRC_CONTEXT" "연결 안 됨" "kubectl context missing"; ok=1; }
  ctx_ok "$DST_CONTEXT" && note "context $DST_CONTEXT" ok || { note "context $DST_CONTEXT" "연결 안 됨" "kubectl context missing"; ok=1; }
  for side in SRC DST; do
    ctx_var="${side}_CONTEXT"; ctx="${!ctx_var}"
    if k "$ctx" get cluster.postgresql.cnpg.io chat-db >/dev/null 2>&1; then note "$ctx chat-db" ok
    else note "$ctx chat-db" "연결 안 됨" "CNPG Cluster chat-db not found in ns $CHAT_NAMESPACE"; ok=1; fi
    if k "$ctx" get "$PGBOUNCER_WORKLOAD" >/dev/null 2>&1; then note "$ctx chat-pgbouncer" ok
    else note "$ctx chat-pgbouncer" "연결 안 됨" "$PGBOUNCER_WORKLOAD not found"; ok=1; fi
    if k "$ctx" get "$ADMIN_SERVICE" >/dev/null 2>&1; then note "$ctx chat-admin" ok
    else note "$ctx chat-admin" "연결 안 됨" "$ADMIN_SERVICE not found"; ok=1; fi
  done
  if k "$SRC_CONTEXT" get "$REVERSE_RELAY_WORKLOAD" >/dev/null 2>&1; then note "reverse relay" ok
  else note "reverse relay" "연결 안 됨" "not deployed (link.sh relay-deploy)"; fi
  if kubectl --context "$PLATFORM_CONTEXT" -n openbao get pod openbao-0 >/dev/null 2>&1; then note "platform OpenBao" ok
  else note "platform OpenBao" "연결 안 됨" "openbao-0 not found on $PLATFORM_CONTEXT"; ok=1; fi
  if kubectl --context "$PLATFORM_CONTEXT" -n bravo-control get pod executor-0 >/dev/null 2>&1; then note "executor-0" ok
  else note "executor-0" "연결 안 됨" "not found on $PLATFORM_CONTEXT"; ok=1; fi
  return $ok
}

case "${1:-check}" in
  check) check || true; log "written: $OUT" ;;
  up)
    check || fail "prerequisites missing (see $OUT)"
    python3 "$CLOUD_DIR/db_roles.py" --env "$DST_ENV" --context "$DST_CONTEXT" --schema dst | tee -a "$OUT"
    python3 "$CLOUD_DIR/db_roles.py" --env "$SRC_ENV" --context "$SRC_CONTEXT" --schema src \
      --app-writer-from "$DST_ENV" | tee -a "$OUT"
    "$CLOUD_DIR/link.sh" relay-deploy
    "$CLOUD_DIR/link.sh" up
    sleep 8
    "$CLOUD_DIR/link.sh" status | tee -a "$OUT"
    "$CLOUD_DIR/repl_setup.sh" setup
    executor_url; curl -fsS "$EXECUTOR_URL/healthz" | tee -a "$OUT"   # executor-0 (platform EKS)
    ;;
  *) fail "usage: bootstrap.sh check | up" ;;
esac
