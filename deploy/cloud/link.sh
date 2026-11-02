#!/usr/bin/env bash
# Temporary link "임시 링크(D-16 전)" (CLOUD-E2E-CONTRACT.md 7, 9): laptop port-forwards and the
# reverse relay agent, each supervised by tools/link (restart, end-to-end health, status JSON).
#   link.sh up | down | status | bao | load | reverse | relay-deploy | relay-delete
# Since D-16, executor-0 and replication use the platform links (bravo-link, env-link) and none of these.
# `load` keeps only the source PgBouncer forward that the laptop load generator writes through.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
(cd "$REPO/tools/link" && uv sync --quiet)
LINK_BIN="$REPO/tools/link/.venv/bin/bravo-link"

start() {  # start <name> <args...>
  local name="$1"; shift
  if [ -f "$STATE/$name.pid" ] && kill -0 "$(cat "$STATE/$name.pid")" 2>/dev/null; then
    log "$name already running (pid $(cat "$STATE/$name.pid"))"; return 0
  fi
  nohup "$LINK_BIN" "$@" --status-file "$STATE/$name.json" > "$STATE/$name.log" 2>&1 &
  echo $! > "$STATE/$name.pid"
  log "$name started (pid $!)"
}
fwd() {  # fwd <name> <context> <resource> <remote> <local> <probe>
  start "$1" forward --name "$1" --context "$2" --namespace "$CHAT_NAMESPACE" --resource "$3" \
    --remote-port "$4" --local-port "$5" --bind-address "$LINK_BIND" --probe "$6"
}

case "${1:-status}" in
  bao)
    # Platform OpenBao on the platform EKS (keys.sh migrate-to-platform), for executor-bao.sh users.
    start bao forward --name bao --context "$PLATFORM_CONTEXT" --namespace openbao --resource pod/openbao-0 \
      --remote-port 8200 --local-port "$BAO_LOCAL_PORT" --bind-address 127.0.0.1 --probe tcp
    ;;
  load)
    # 임시 링크 (부하 생성기 전용): laptop loadgen -> source PgBouncer. Not on any platform path.
    fwd src-pgb "$SRC_CONTEXT" "$PGBOUNCER_SERVICE" 6432 "$SRC_PGB_PORT" tcp
    ;;
  up)
    ctx_ok "$SRC_CONTEXT" || fail "kubectl context $SRC_CONTEXT missing"
    ctx_ok "$DST_CONTEXT" || fail "kubectl context $DST_CONTEXT missing"
    fwd src-db      "$SRC_CONTEXT" "$DB_RW_SERVICE" 5432 "$SRC_DB_PORT" pg
    fwd src-db-repl "$SRC_CONTEXT" "$DB_RW_SERVICE" 5432 "$SRC_DB_REPL_PORT" pg
    fwd src-db-app  "$SRC_CONTEXT" "$DB_RW_SERVICE" 5432 "$SRC_DB_APP_PORT" pg
    fwd dst-db      "$DST_CONTEXT" "$DB_RW_SERVICE" 5432 "$DST_DB_PORT" pg
    fwd src-pgb     "$SRC_CONTEXT" "$PGBOUNCER_SERVICE" 6432 "$SRC_PGB_PORT" tcp
    fwd dst-pgb     "$DST_CONTEXT" "$PGBOUNCER_SERVICE" 6432 "$DST_PGB_PORT" tcp
    fwd src-admin   "$SRC_CONTEXT" "$ADMIN_SERVICE" 8081 "$SRC_ADMIN_PORT" tcp
    fwd dst-admin   "$DST_CONTEXT" "$ADMIN_SERVICE" 8081 "$DST_ADMIN_PORT" tcp
    "$0" reverse
    ;;
  reverse)
    start reverse reverse --relay-context "$SRC_CONTEXT" --relay-namespace "$CHAT_NAMESPACE" \
      --relay-resource "$REVERSE_RELAY_WORKLOAD" --relay-agent-port 7070 --relay-local-port "$REVERSE_AGENT_PORT" \
      --target-context "$DST_CONTEXT" --target-namespace "$CHAT_NAMESPACE" --target-resource "$DB_RW_SERVICE" \
      --target-remote-port 5432 --target-local-port "$REVERSE_TARGET_PORT" --pool 8
    ;;
  relay-deploy)
    # The in-cluster half of the reverse tunnel (ns chat on the source cluster), image by digest.
    sed "s#image: bravo-link-relay#image: ${RELAY_IMAGE}#" "$REPO/tools/link/k8s/reverse-relay.yaml" \
      | kubectl --context "$SRC_CONTEXT" -n "$CHAT_NAMESPACE" apply -f -
    kubectl --context "$SRC_CONTEXT" -n "$CHAT_NAMESPACE" rollout status "$REVERSE_RELAY_WORKLOAD" --timeout=180s
    ;;
  relay-delete)
    kubectl --context "$SRC_CONTEXT" -n "$CHAT_NAMESPACE" delete -f "$REPO/tools/link/k8s/reverse-relay.yaml" --ignore-not-found
    ;;
  down)
    for f in "$STATE"/*.pid; do
      [ -e "$f" ] || continue
      kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; log "stopped $(basename "$f" .pid)"
    done
    ;;
  status)
    for f in "$STATE"/*.json; do
      [ -e "$f" ] || continue
      case "$(basename "$f")" in topology.json) continue ;; esac
      jq -c '{name: (.name // input_filename), label, state, restarts, last_ok, last_error}' "$f" 2>/dev/null || cat "$f"
    done
    ;;
  *) fail "usage: link.sh up|down|status" ;;
esac
