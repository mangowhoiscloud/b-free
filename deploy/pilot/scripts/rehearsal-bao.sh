#!/usr/bin/env bash
# Rehearsal service credentials from the platform OpenBao (D-26), same custody model as keys.sh and
# executor-bao.sh. This script never prints a token, secret_id, password or DSN.
#
#   rehearsal-bao.sh setup                    policy platform-rehearsal (bao/policies/platform-rehearsal.hcl),
#                                             platform-rehearsal-issuer, AppRole rehearsal, issuer token -> custody
#   rehearsal-bao.sh put-evidence-db          .env PLATFORM_DB_REHEARSAL_RO_PASSWORD -> secret/platform/rehearsal-evidence-db
#   rehearsal-bao.sh put-synthetic-db         .env REHEARSAL_SYNTHETIC_RW_PASSWORD -> secret/platform/rehearsal-synthetic-db
#   rehearsal-bao.sh put-collector-ro ENV     stdin lines host=,port=,dbname=,user=,password=[,sslmode=]
#                                             -> secret/envs/ENV/collector-ro (read-only catalog role)
#   rehearsal-bao.sh with-rehearsal -- CMD    run CMD with BRAVO_BAO_ROLE_ID / BRAVO_BAO_SECRET_ID
#                                             (one fresh secret_id: 60 s, 1 use) in its env only
#   rehearsal-bao.sh status                   policy, role settings, issuer token, which secrets exist (no values)
#   rehearsal-bao.sh --target pilot|platform-eks|platform-rke2 <cmd>   which OpenBao (default: custody bao_target,
#                                             through a private kubectl port-forward kept open for <cmd>)
#
# The service logs in once at start (bravo_rehearsal.server_tools.OpenBao.login), drops the
# secret_id, and renews its periodic token (1 h period) while it runs. A restart needs a new
# secret_id: run the start command through with-rehearsal again.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PILOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PILOT_DIR/.env"
TLS_DIR="$PILOT_DIR/bao/tls"
POLICY_FILE="$PILOT_DIR/bao/policies/platform-rehearsal.hcl"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
CUSTODY="$CUSTODY_DIR/openbao-init.json"
ROLE=rehearsal

die() { printf 'rehearsal-bao.sh: %s\n' "$*" >&2; exit 1; }
log() { printf 'rehearsal-bao.sh: %s\n' "$*"; }

# Value of KEY in .env without sourcing it. Only ever captured with $(...), never printed.
env_get() {
  [[ -f $ENV_FILE ]] || return 0
  local v
  v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)"
  v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  printf '%s' "$v"
}

BAO_PORT="${PLATFORM_BAO_HOST_PORT:-$(env_get PLATFORM_BAO_HOST_PORT)}"
BAO_ADDR="${BAO_ADDR:-https://127.0.0.1:${BAO_PORT:-8200}}"

# Target, same rule as keys.sh (D-26: one store): pilot = laptop Compose platform-bao; platform-eks = OpenBao on the
# platform EKS, reached only through a private `kubectl port-forward` to openbao/openbao-0 for this run. Choose with
# `rehearsal-bao.sh --target <t> <cmd>` or BRAVO_BAO_TARGET; default platform-eks once custody records the move.
# platform-rke2 = the spec §7.2 platform (EC2 + RKE2 node, kube API via the SSM port forward); platform-eks = the
# interim EKS platform. Context per target: BRAVO_PLATFORM_CONTEXT overrides. Default: custody's bao_target.
BAO_TARGET="${BRAVO_BAO_TARGET:-}"
if [[ ${1-} == --target ]]; then BAO_TARGET=${2-}; shift 2 || die "usage: rehearsal-bao.sh --target pilot|platform-eks|platform-rke2 <cmd>"
elif [[ ${1-} == --target=* ]]; then BAO_TARGET=${1#--target=}; shift; fi
if [[ -z $BAO_TARGET ]]; then
  BAO_TARGET=pilot
  [[ -f $CUSTODY ]] && BAO_TARGET="$(jq -r '.bao_target // "pilot"' "$CUSTODY" 2>/dev/null || echo pilot)"
fi
case $BAO_TARGET in
  platform-eks) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-bravo-platform}"; PLATFORM_TLS_DIR="$CUSTODY_DIR/platform-eks" ;;
  platform-rke2) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"; PLATFORM_TLS_DIR="$CUSTODY_DIR/platform-rke2" ;;
  pilot) PLATFORM_CONTEXT=""; PLATFORM_TLS_DIR="" ;;
  *) die "unknown target: $BAO_TARGET (pilot | platform-eks | platform-rke2)" ;;
esac
PF_PID=""
pf_stop() { [[ -z $PF_PID ]] || { kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; PF_PID=""; }; }
pf_start() {
  [[ -z $PF_PID ]] || return 0
  command -v kubectl >/dev/null || die "--target $BAO_TARGET needs kubectl"
  [[ -f $PLATFORM_TLS_DIR/ca.crt ]] || die "no $PLATFORM_TLS_DIR/ca.crt: keys.sh --target $BAO_TARGET tls"
  local port i code
  port="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
  kubectl --context "$PLATFORM_CONTEXT" -n openbao port-forward pod/openbao-0 "$port:8200" >/dev/null 2>&1 &
  PF_PID=$!
  trap pf_stop EXIT
  TLS_DIR="$PLATFORM_TLS_DIR"; BAO_ADDR="https://127.0.0.1:$port"
  for i in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$TLS_DIR/ca.crt" --max-time 2 "$BAO_ADDR/v1/sys/seal-status" || true)"
    [[ $code == 200 ]] && return 0
    kill -0 "$PF_PID" 2>/dev/null || die "kubectl port-forward to $PLATFORM_CONTEXT openbao/openbao-0 exited"
    sleep 0.25
  done
  die "platform OpenBao not reachable through port-forward ($PLATFORM_CONTEXT openbao/openbao-0)"
}

tok_hdr() { [[ -z ${1-} ]] || printf 'X-Vault-Token: %s\n' "$1"; }

# api [-d] METHOD PATH [TOKEN]; -d: JSON body on stdin. The token goes to curl through a pipe, never argv.
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
    printf 'rehearsal-bao.sh: %s %s -> HTTP %s\n' "$method" "$path" "$code" >&2
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

POLICY_REHEARSAL_ISSUER='# Mints short-lived secret_ids for the rehearsal AppRole only (rehearsal-bao.sh with-rehearsal).
path "auth/approle/role/rehearsal/role-id" { capabilities = ["read"] }
path "auth/approle/role/rehearsal/secret-id" { capabilities = ["update"] }'

cmd_setup() {
  need_custody; need_unsealed
  [[ -f $POLICY_FILE ]] || die "missing $POLICY_FILE"
  local admin; admin="$(custody_get .admin_token)"
  jq -Rs '{policy: .}' "$POLICY_FILE" | api -d PUT sys/policies/acl/platform-rehearsal "$admin" >/dev/null
  log "policy platform-rehearsal ($POLICY_FILE)"
  jq -n --arg p "$POLICY_REHEARSAL_ISSUER" '{policy: $p}' \
    | api -d PUT sys/policies/acl/platform-rehearsal-issuer "$admin" >/dev/null
  log "policy platform-rehearsal-issuer"
  # Periodic service token: the long-running service renews it; no re-login, no stored secret_id.
  printf '%s' '{"token_policies":["platform-rehearsal"],"token_type":"service","token_period":"1h",
    "bind_secret_id":true,"secret_id_ttl":"60s","secret_id_num_uses":1}' \
    | api -d POST "auth/approle/role/$ROLE" "$admin" >/dev/null
  log "approle: role $ROLE (policy platform-rehearsal, periodic token 1h, secret_id 60s / 1 use)"
  local existing=""
  existing="$(jq -r '.rehearsal_issuer_token // ""' "$CUSTODY")"
  if [[ -n $existing ]] && api POST auth/token/renew-self "$existing" >/dev/null 2>&1; then
    log "issuer token: present in custody and renewed"
  else
    local out
    if out="$(printf '{"policies":["platform-rehearsal-issuer"],"period":"168h","display_name":"rehearsal-issuer"}' \
        | api -d POST auth/token/create-orphan "$admin" 2>/dev/null)"; then
      printf '%s' "$out" \
        | jq '{rehearsal_issuer_token: .auth.client_token, rehearsal_issuer_token_accessor: .auth.accessor}' | custody_merge
      log "issuer token: platform-rehearsal-issuer (periodic 168h) -> custody"
    else
      # The platform-admin policy cannot create orphan tokens on this store; with-rehearsal then mints the
      # secret_id with the admin token from custody, as keys.sh llm-run does.
      log "issuer token: not created (create-orphan refused); with-rehearsal will use the custody admin token"
    fi
  fi
}

# put_db PATH HOST PORT DBNAME USER: password on stdin (one line), written with the admin token.
put_db() {
  local path=$1 host=$2 port=$3 db=$4 user=$5 admin
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  jq -Rn --arg host "$host" --arg port "$port" --arg db "$db" --arg user "$user" \
    '{data: {host: $host, port: $port, dbname: $db, user: $user, password: input, sslmode: "disable"}}' \
    | api -d POST "secret/data/$path" "$admin" \
    | jq -r '"written secret/'"$path"' version \(.data.version) (password not shown)"'
}

cmd_put_evidence_db() {
  local pw db; pw="$(env_get PLATFORM_DB_REHEARSAL_RO_PASSWORD)"; db="$(env_get PLATFORM_DB_NAME)"
  [[ -n $pw && -n $db ]] || die "set PLATFORM_DB_REHEARSAL_RO_PASSWORD and PLATFORM_DB_NAME in $ENV_FILE"
  printf '%s\n' "$pw" | put_db platform/rehearsal-evidence-db platform-db 5432 "$db" bravo_rehearsal_ro
}

cmd_put_synthetic_db() {
  local pw; pw="$(env_get REHEARSAL_SYNTHETIC_RW_PASSWORD)"
  [[ -n $pw ]] || die "set REHEARSAL_SYNTHETIC_RW_PASSWORD in $ENV_FILE"
  printf '%s\n' "$pw" | put_db platform/rehearsal-synthetic-db rehearsal-synthetic-db 5432 synthetic rehearse_rw
}

cmd_put_collector_ro() {
  local env=${1-} admin
  [[ $env =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || die "usage: rehearsal-bao.sh put-collector-ro <env> < kv-lines"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  jq -Rn '[inputs | select(test("^(host|port|dbname|user|password|sslmode)=")) | capture("^(?<k>[a-z]+)=(?<v>.*)$")]
          | from_entries as $d | if ($d | has("host") and has("dbname") and has("user") and has("password"))
          then {data: $d} else error("need host, dbname, user, password") end' \
    | api -d POST "secret/data/envs/$env/collector-ro" "$admin" \
    | jq -r '"written secret/envs/'"$env"'/collector-ro version \(.data.version) (values not shown)"'
}

cmd_with_rehearsal() {
  [[ ${1-} == -- ]] && shift
  (($#)) || die "usage: rehearsal-bao.sh with-rehearsal -- <cmd> [args...]"
  need_custody; need_unsealed
  local issuer role_id secret_id
  issuer="$(jq -r '.rehearsal_issuer_token // ""' "$CUSTODY")"
  if [[ -z $issuer ]] || ! api POST auth/token/renew-self "$issuer" >/dev/null 2>&1; then
    issuer="$(custody_get .admin_token)"  # narrow issuer token unavailable on this store (see setup)
  fi
  role_id="$(api GET "auth/approle/role/$ROLE/role-id" "$issuer" | jq -er .data.role_id)"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST "auth/approle/role/$ROLE/secret-id" "$issuer" | jq -er .data.secret_id)"
  # The command keeps the port-forward for its whole run (platform-eks), so it is not exec'd.
  BRAVO_BAO_ADDR="$BAO_ADDR" BRAVO_BAO_CACERT="$TLS_DIR/ca.crt" \
    BRAVO_BAO_ROLE_ID="$role_id" BRAVO_BAO_SECRET_ID="$secret_id" "$@"
}

cmd_status() {
  need_custody; need_unsealed
  local admin p; admin="$(custody_get .admin_token)"
  if api GET sys/policies/acl/platform-rehearsal "$admin" >/dev/null 2>&1; then log "policy platform-rehearsal: present"; else log "policy platform-rehearsal: missing"; fi
  api GET "auth/approle/role/$ROLE" "$admin" 2>/dev/null \
    | jq -c '.data | {token_policies, token_period, secret_id_ttl, secret_id_num_uses}' \
    || log "approle $ROLE: missing"
  if [[ -n $(jq -r '.rehearsal_issuer_token // ""' "$CUSTODY") ]]; then log "issuer token: in custody"; else log "issuer token: none"; fi
  for p in platform/rehearsal-evidence-db platform/rehearsal-synthetic-db; do
    if api GET "secret/metadata/$p" "$admin" >/dev/null 2>&1; then log "secret/$p: written"; else log "secret/$p: missing"; fi
  done
}

for bin in curl jq; do command -v "$bin" >/dev/null || die "needs $bin"; done
cmd=${1-help}; shift || true
[[ $BAO_TARGET == pilot || $cmd == help || $cmd == -h || $cmd == --help ]] || pf_start
case $cmd in
  setup) cmd_setup ;;
  put-evidence-db) cmd_put_evidence_db ;;
  put-synthetic-db) cmd_put_synthetic_db ;;
  put-collector-ro) cmd_put_collector_ro "$@" ;;
  with-rehearsal) cmd_with_rehearsal "$@" ;;
  status) cmd_status ;;
  help | -h | --help) sed -n '2,21p' "${BASH_SOURCE[0]}" ;;
  *) die "unknown command: $cmd (rehearsal-bao.sh help)" ;;
esac
