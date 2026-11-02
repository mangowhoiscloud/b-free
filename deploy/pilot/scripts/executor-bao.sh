#!/usr/bin/env bash
# Executor credentials from the platform OpenBao (D-26), same custody model as keys.sh.
# This script never prints a token, secret_id or secret value.
#
#   executor-bao.sh setup                 policies platform-executor(-issuer), AppRole executor,
#                                         periodic issuer token -> custody (executor_issuer_token)
#   executor-bao.sh with-executor -- CMD  run CMD with BRAVO_BAO_ROLE_ID / BRAVO_BAO_SECRET_ID
#                                         (one fresh secret_id: 60 s, 1 use) in its env only
#   executor-bao.sh status                policy, role settings, issuer token presence (no values)
#
# The executor logs in with that pair (bravo_executor.vault), holds a 60 min token for one run,
# reads only the paths in POLICY_EXECUTOR, and revokes the token when the run ends.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PILOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PILOT_DIR/.env"
TLS_DIR="$PILOT_DIR/bao/tls"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
CUSTODY="$CUSTODY_DIR/openbao-init.json"
ROLE=executor

die() { printf 'executor-bao.sh: %s\n' "$*" >&2; exit 1; }
log() { printf 'executor-bao.sh: %s\n' "$*"; }

env_get() {
  [[ -f $ENV_FILE ]] || return 0
  local v
  v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)"
  v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"
  printf '%s' "$v"
}

BAO_PORT="${PLATFORM_BAO_HOST_PORT:-$(env_get PLATFORM_BAO_HOST_PORT)}"
BAO_ADDR="${BAO_ADDR:-https://127.0.0.1:${BAO_PORT:-8200}}"
# Target (D-26: one store): after keys.sh migrate-to-platform the store is the platform EKS OpenBao
# (custody bao_target = platform-eks). Like keys.sh, this script opens a private port-forward to
# pod openbao-0 on a free local port for this invocation only and closes it on exit; there is no
# 127.0.0.1:8200 default any more. BRAVO_BAO_TARGET=pilot reaches the stopped laptop store.
PF_PID=""
pf_stop() { [[ -z $PF_PID ]] || { kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; PF_PID=""; }; }
BAO_TARGET="${BRAVO_BAO_TARGET:-}"
if [[ -z $BAO_TARGET ]]; then
  BAO_TARGET=pilot
  [[ -f $CUSTODY && $(jq -r '.bao_target // "pilot"' "$CUSTODY" 2>/dev/null) == platform-eks ]] && BAO_TARGET=platform-eks
fi
if [[ $BAO_TARGET == platform-eks ]]; then
  command -v kubectl >/dev/null || die "platform-eks target needs kubectl"
  TLS_DIR="$CUSTODY_DIR/platform-eks"
  [[ -f $TLS_DIR/ca.crt ]] || die "no $TLS_DIR/ca.crt (keys.sh --target platform-eks tls)"
  pf_port="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
  kubectl --context "${BRAVO_PLATFORM_CONTEXT:-bravo-platform}" -n openbao port-forward pod/openbao-0 \
    "$pf_port:8200" >/dev/null 2>&1 &
  PF_PID=$!
  trap pf_stop EXIT
  BAO_ADDR="https://127.0.0.1:$pf_port"
  for _ in $(seq 1 40); do
    [[ $(curl -s -o /dev/null -w '%{http_code}' --cacert "$TLS_DIR/ca.crt" --max-time 2 "$BAO_ADDR/v1/sys/seal-status" || true) == 200 ]] && break
    kill -0 "$PF_PID" 2>/dev/null || die "port-forward to openbao-0 exited"
    sleep 0.25
  done
fi

tok_hdr() { [[ -z ${1-} ]] || printf 'X-Vault-Token: %s\n' "$1"; }

# api [-d] METHOD PATH [TOKEN]; -d: JSON body on stdin. Token goes to curl through a pipe.
api() {
  local body=0
  [[ $1 == -d ]] && { body=1; shift; }
  local method=$1 path=$2 token=${3-} out code
  if ((body)); then
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -H "Content-Type: application/json" --data-binary @- -w '\n%{http_code}' "$BAO_ADDR/v1/$path")" \
      || die "cannot reach OpenBao at $BAO_ADDR"
  else
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -w '\n%{http_code}' "$BAO_ADDR/v1/$path" </dev/null)" || die "cannot reach OpenBao at $BAO_ADDR"
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  if [[ $code != 2* ]]; then
    printf 'executor-bao.sh: %s %s -> HTTP %s\n' "$method" "$path" "$code" >&2
    return 1
  fi
  printf '%s' "$out"
}

file_mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }
need_custody() {
  [[ -f $CUSTODY ]] || die "no custody file at $CUSTODY"
  [[ $(file_mode "$CUSTODY") == 600 ]] || die "custody file must be mode 600"
  [[ $(file_mode "$CUSTODY_DIR") == 700 ]] || die "custody dir must be mode 700"
}
custody_get() { jq -er "$1" "$CUSTODY"; }
custody_merge() {
  local tmp; tmp="$(mktemp "$CUSTODY_DIR/.custody.XXXXXX")"
  if jq -s '.[0] * .[1]' "$CUSTODY" - >"$tmp"; then mv -f "$tmp" "$CUSTODY"; else rm -f "$tmp"; die "custody update failed"; fi
}
need_unsealed() {
  local st; st="$(api GET sys/seal-status)"
  [[ $(printf '%s' "$st" | jq -r .sealed) == false ]] || die "OpenBao is sealed: scripts/keys.sh unseal"
}

POLICY_EXECUTOR='# Executor (D-19, D-26): read-only, one run at a time. Nothing else.
path "secret/data/platform/cloudflare" { capabilities = ["read"] }
path "secret/data/envs/+/chat-admin" { capabilities = ["read"] }
path "secret/data/envs/+/chat-db" { capabilities = ["read"] }
path "secret/data/envs/+/object-store" { capabilities = ["read"] }'

POLICY_EXECUTOR_ISSUER='# Mints short-lived secret_ids for the executor AppRole only (executor-bao.sh with-executor).
path "auth/approle/role/executor/role-id" { capabilities = ["read"] }
path "auth/approle/role/executor/secret-id" { capabilities = ["update"] }'

cmd_setup() {
  need_custody; need_unsealed
  local admin; admin="$(custody_get .admin_token)"
  jq -n --arg p "$POLICY_EXECUTOR" '{policy: $p}' | api -d PUT sys/policies/acl/platform-executor "$admin" >/dev/null
  log "policy platform-executor"
  jq -n --arg p "$POLICY_EXECUTOR_ISSUER" '{policy: $p}' | api -d PUT sys/policies/acl/platform-executor-issuer "$admin" >/dev/null
  log "policy platform-executor-issuer"
  printf '%s' '{"token_policies":["platform-executor"],"token_type":"service","token_ttl":"60m","token_max_ttl":"120m",
    "bind_secret_id":true,"secret_id_ttl":"60s","secret_id_num_uses":1}' \
    | api -d POST "auth/approle/role/$ROLE" "$admin" >/dev/null
  log "approle: role $ROLE (policy platform-executor, token 60m/120m, secret_id 60s / 1 use)"

  local existing=""
  existing="$(jq -r '.executor_issuer_token // ""' "$CUSTODY")"
  if [[ -n $existing ]] && api POST auth/token/renew-self "$existing" >/dev/null 2>&1; then
    log "issuer token: present in custody and renewed"
  else
    # An orphan token with a policy the admin token does not hold needs root (as in keys.sh init).
    # Break-glass: scripts/keys.sh generate-root, then this setup, then scripts/keys.sh revoke-root.
    [[ $(jq -r '.root_token_revoked_at // ""' "$CUSTODY") == "" ]] \
      || die "issuer token missing: run scripts/keys.sh generate-root, then setup again, then scripts/keys.sh revoke-root"
    local root; root="$(custody_get .root_token)"
    printf '{"policies":["platform-executor-issuer"],"period":"168h","display_name":"executor-issuer"}' \
      | api -d POST auth/token/create-orphan "$root" \
      | jq -e '{executor_issuer_token: .auth.client_token, executor_issuer_token_accessor: .auth.accessor}' | custody_merge
    log "issuer token: platform-executor-issuer (periodic 168h) -> custody. Now run: scripts/keys.sh revoke-root"
  fi
}

cmd_with_executor() {
  [[ ${1-} == -- ]] && shift
  (($#)) || die "usage: executor-bao.sh with-executor -- <cmd> [args...]"
  need_custody; need_unsealed
  local issuer role_id secret_id
  issuer="$(custody_get .executor_issuer_token)" || die "no executor_issuer_token: executor-bao.sh setup"
  api POST auth/token/renew-self "$issuer" >/dev/null || die "executor issuer token invalid: executor-bao.sh setup"
  role_id="$(api GET "auth/approle/role/$ROLE/role-id" "$issuer" | jq -er .data.role_id)"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST "auth/approle/role/$ROLE/secret-id" "$issuer" | jq -er .data.secret_id)"
  # Not exec: the private port-forward must live as long as CMD and be closed after it.
  local rc=0
  BRAVO_BAO_ADDR="$BAO_ADDR" BRAVO_BAO_CACERT="$TLS_DIR/ca.crt" \
    BRAVO_BAO_ROLE_ID="$role_id" BRAVO_BAO_SECRET_ID="$secret_id" "$@" || rc=$?
  pf_stop
  return $rc
}

cmd_status() {
  need_custody; need_unsealed
  local admin; admin="$(custody_get .admin_token)"
  if api GET sys/policies/acl/platform-executor "$admin" >/dev/null 2>&1; then log "policy platform-executor: present"; else log "policy platform-executor: missing"; fi
  api GET "auth/approle/role/$ROLE" "$admin" 2>/dev/null \
    | jq -c '.data | {token_policies, token_ttl, token_max_ttl, secret_id_ttl, secret_id_num_uses}' \
    || log "approle $ROLE: missing"
  if [[ -n $(jq -r '.executor_issuer_token // ""' "$CUSTODY") ]]; then log "issuer token: in custody"; else log "issuer token: none"; fi
}

for bin in curl jq; do command -v "$bin" >/dev/null || die "needs $bin"; done
cmd=${1-help}; shift || true
case $cmd in
  setup) cmd_setup ;;
  with-executor) cmd_with_executor "$@" ;;
  status) cmd_status ;;
  help | -h | --help) sed -n '2,12p' "${BASH_SOURCE[0]}" ;;
  *) die "unknown command: $cmd (executor-bao.sh help)" ;;
esac
