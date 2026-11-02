#!/usr/bin/env bash
# Writes $STATE/topology.json (no secrets) from cloud.env for the Executor and repl_setup.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
jq -n \
  --arg label "$LINK_LABEL" --arg db "$CHAT_DB_NAME" --arg ns "$CHAT_NAMESPACE" \
  --arg srcctx "$SRC_CONTEXT" --arg dstctx "$DST_CONTEXT" --arg pgbw "$PGBOUNCER_WORKLOAD" \
  --arg srcenv "$SRC_ENV" --arg dstenv "$DST_ENV" \
  --arg srcapp "$SRC_APP_ENV_ID" --arg dstapp "$DST_APP_ENV_ID" \
  --arg srcstore "$SRC_STORE_ID" --arg dststore "$DST_STORE_ID" \
  --arg bind "$LINK_BIND" --arg lima "$LIMA_HOST_NAME" --arg relay "$REVERSE_RELAY_SERVICE_HOST" \
  --argjson srcdb "$SRC_DB_PORT" --argjson dstdb "$DST_DB_PORT" --argjson repl "$SRC_DB_REPL_PORT" \
  --argjson srcpgb "$SRC_PGB_PORT" --argjson dstpgb "$DST_PGB_PORT" \
  --argjson srcadm "$SRC_ADMIN_PORT" --argjson dstadm "$DST_ADMIN_PORT" '
{
  label: $label, chat_db: $db,
  store_ids: {src: $srcstore, dst: $dststore},
  cluster_names: {src: "chat-db", dst: "chat-db"},
  db: {
    src: {conninfo: "host=\($bind) port=\($srcdb)", secret: "envs/\($srcenv)/chat-db"},
    dst: {conninfo: "host=\($bind) port=\($dstdb)", secret: "envs/\($dstenv)/chat-db"}
  },
  pools: [
    {name: "src", conninfo: "host=\($bind) port=\($srcpgb)", secret: "envs/\($srcenv)/chat-db",
     switch: {kind: "k8s-exec", context: $srcctx, namespace: $ns, workload: $pgbw}},
    {name: "dst", conninfo: "host=\($bind) port=\($dstpgb)", secret: "envs/\($dstenv)/chat-db",
     switch: {kind: "k8s-exec", context: $dstctx, namespace: $ns, workload: $pgbw}}
  ],
  entries: [
    {name: $srcapp, side: "src", url: "http://\($bind):\($srcadm)", secret: "envs/\($srcenv)/chat-admin"},
    {name: $dstapp, side: "dst", url: "http://\($bind):\($dstadm)", secret: "envs/\($dstenv)/chat-admin"}
  ],
  object_stores: {
    src: {secret: "envs/\($srcenv)/object-store"},
    dst: {secret: "envs/\($dstenv)/object-store"}
  },
  replication: {
    fwd_conninfo: "host=\($lima) port=\($repl)",
    rev_conninfo: "host=\($relay) port=5432",
    label: $label
  }
}' > "$STATE/topology.json"
log "topology -> $STATE/topology.json"
