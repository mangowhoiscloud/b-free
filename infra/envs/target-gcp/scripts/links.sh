#!/usr/bin/env bash
# Temporary link (D-16 전, CLOUD-E2E-CONTRACT §7) between the GKE landing zone and the platform.
# Uses tools/link (bravo-link): an in-cluster relay per service, and a laptop agent that dials the relay's
# agent port through `kubectl port-forward` and splices each paired connection to the target.
#
#   name           in-cluster Service (ns)                 laptop target
#   openbao        openbao:8200 (external-secrets)          platform OpenBao (ESO, D-26): port-forward to
#                                                           bravo-platform openbao/openbao-0, or the pilot
#                                                           127.0.0.1:8200 while custody says bao_target=pilot
#   platform-otel  platform-otel:4317 (bravo-observability) platform Collector: port-forward to bravo-platform
#                                                           bravo-observe/svc/otel-collector:4317, or
#                                                           BRAVO_PLATFORM_OTLP_ADDR (host:port on the laptop)
#   src-db-link    src-db-link:5432 (chat)                  source DB app path the platform opens on the laptop
#                                                           (CLOUD-E2E-CONTRACT §9: 127.0.0.1:15434)
#
#   links.sh render   print the relay manifests (image pinned from versions.env)
#   links.sh start    start the laptop agents that have a target (pid + status in $STATE_DIR)
#   links.sh stop     stop them
#   links.sh status   process state and relay stats (no secrets)
source "$(dirname "$0")/lib.sh"

LINK_BIN=("$REPO_ROOT/tools/link/.venv/bin/bravo-link")
[[ -x ${LINK_BIN[0]} ]] || LINK_BIN=(uv run --project "$REPO_ROOT/tools/link" bravo-link)
SRC_DB_ADDR="${BRAVO_SRC_DB_APP_ADDR:-127.0.0.1:15434}"
OTLP_ADDR="${BRAVO_PLATFORM_OTLP_ADDR:-}"

# name namespace client_port local_relay_port
LINKS=(
  "openbao external-secrets 8200 17181"
  "platform-otel bravo-observability 4317 17182"
  "src-db-link chat 5432 17183"
)

cmd_render() {
  local l name ns port
  for l in "${LINKS[@]}"; do
    read -r name ns port _ <<<"$l"
    "${LINK_BIN[@]}" render-relay --namespace "$ns" --name "$name" --client-port "$port" --agent-port 7070 \
      --image "$LINK_RELAY_IMAGE"
    printf '\n---\n'
  done
}

pidfile() { printf '%s/link-%s.pid' "$STATE_DIR" "$1"; }
running() { local p; p="$(cat "$(pidfile "$1")" 2>/dev/null)" && kill -0 "$p" 2>/dev/null; }

start_one() {   # start_one NAME NS LOCALPORT -- target args...
  local name=$1 ns=$2 lport=$3; shift 4
  if running "$name"; then log "link $name already running (pid $(cat "$(pidfile "$name")"))"; return 0; fi
  nohup "${LINK_BIN[@]}" reverse --relay-context "$CLUSTER" --relay-namespace "$ns" --relay-resource "deploy/$name" \
    --relay-agent-port 7070 --relay-local-port "$lport" --pool 4 \
    --status-file "$STATE_DIR/link-$name.json" --name "$name" "$@" \
    >>"$STATE_DIR/link-$name.log" 2>&1 &
  echo $! >"$(pidfile "$name")"
  log "link $name started (pid $!, log $STATE_DIR/link-$name.log)"
}

cmd_start() {
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  if [[ $BAO_TARGET == platform-eks ]]; then
    start_one openbao external-secrets 17181 -- --target-context "$PLATFORM_CONTEXT" --target-namespace openbao \
      --target-resource pod/openbao-0 --target-remote-port 8200 --target-local-port 18210 --target-probe tcp
  else
    start_one openbao external-secrets 17181 -- --target 127.0.0.1:8200
  fi
  if [[ -n $OTLP_ADDR ]]; then
    start_one platform-otel bravo-observability 17182 -- --target "$OTLP_ADDR"
  else
    kubectl --context "$PLATFORM_CONTEXT" -n bravo-observe get svc otel-collector >/dev/null 2>&1 \
      || log "platform-otel: $PLATFORM_CONTEXT has no bravo-observe/otel-collector yet (연결 안 됨); agent retries"
    start_one platform-otel bravo-observability 17182 -- --target-context "$PLATFORM_CONTEXT" \
      --target-namespace bravo-observe --target-resource svc/otel-collector --target-remote-port 4317 \
      --target-local-port 14317 --target-probe tcp
  fi
  # The agent dials the target per paired connection, so it can start before the platform opens 15434.
  start_one src-db-link chat 17183 -- --target "$SRC_DB_ADDR"
  nc -z -w 2 "${SRC_DB_ADDR%:*}" "${SRC_DB_ADDR##*:}" 2>/dev/null \
    || log "src-db-link: nothing listens on $SRC_DB_ADDR yet (연결 안 됨: source DB app forward not open)"
}

cmd_stop() {
  local l name
  for l in "${LINKS[@]}"; do
    read -r name _ <<<"$l"
    if running "$name"; then
      kill "$(cat "$(pidfile "$name")")" && log "link $name stopped"
    fi
    rm -f "$(pidfile "$name")"
  done
}

cmd_status() {
  local l name lport
  for l in "${LINKS[@]}"; do
    read -r name _ _ lport <<<"$l"
    if running "$name"; then
      printf '%s: running pid %s; relay %s\n' "$name" "$(cat "$(pidfile "$name")")" \
        "$("${LINK_BIN[@]}" probe-relay --relay-addr "127.0.0.1:$lport" 2>/dev/null || echo 'probe failed')"
    else
      printf '%s: not running\n' "$name"
    fi
  done
}

load_outputs
case ${1-} in
  render) cmd_render ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  *) die "usage: links.sh render|start|stop|status" ;;
esac
