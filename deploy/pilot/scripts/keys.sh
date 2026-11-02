#!/usr/bin/env bash
# Platform key store (D-26): the platform OpenBao (compose service platform-bao) is the only
# holder of keys and secrets. This script never prints a token, share or secret value.
#
#   keys.sh tls [--rotate]          pilot CA + server cert into deploy/pilot/bao/tls (gitignored, 600)
#   keys.sh init                    Shamir 3/2 -> operator custody, KV v2, policies, AppRole, audit, revoke root
#   keys.sh unseal                  unseal with the shares in custody
#   keys.sh status                  seal state, custody, audit devices, Cloudflare secret version
#   keys.sh import-cloudflare       .env CLOUDFLARE_* -> secret/platform/cloudflare, then blank the .env token
#   keys.sh with-cloudflare -- CMD  run CMD with CLOUDFLARE_API_TOKEN/ACCOUNT_ID/ZONE_ID in its env only
#   keys.sh issue-tunnel-token ENV TUNNEL_ID [NAME]  tunnel token -> secret/envs/ENV/NAME (default cf-tunnel)
#   keys.sh issue-service-token ENV NAME [SECRET] [DUR]  Access service token -> secret/envs/ENV/SECRET
#                                   (default cf-access-service-token, 168h); prints only the token ID
#   keys.sh generate-root | revoke-root        break-glass root token (custody only) and its revocation
#   keys.sh env-generate ENV NAME FIELD [BYTES] random value -> secret/envs/ENV/NAME (only if absent)
#   keys.sh env-pipe ENV NAME FIELD -- CMD      CMD receives secret/envs/ENV/NAME FIELD on stdin only
#   keys.sh env-copy SRC NAME FIELD DST         copy one field secret/envs/SRC/NAME -> secret/envs/DST/NAME (pipes only)
#   keys.sh llm-run claude|openai -- CMD        CMD gets a one-use secret_id (60 s) for AppRole llm-<p> (reads only
#                                   secret/envs/platform/llm-<p>) and the OpenBao address; it reads the credential itself
#   keys.sh llm-status                          llm-claude / llm-openai stored or not (metadata only, no value)
#   keys.sh llm-login openai [--url-file F]     demo: the platform's own ChatGPT session via `codex login` in a temp
#                                   CODEX_HOME (deleted after) -> secret/envs/platform/llm-openai (scripts/codex_login.py)
#   keys.sh env-put-json ENV NAME               stdin JSON object of string fields -> secret/envs/ENV/NAME (merged;
#                                   null removes a field); prints field names only
#   keys.sh env-put ENV NAME FIELD              stdin -> secret/envs/ENV/NAME FIELD (never overwrites a different value)
#   keys.sh signer-init                         T31: transit engine + non-exportable key bravo-supply-chain (ecdsa-p256),
#                                               policy platform-signer(-issuer), AppRole signer, issuer token -> custody
#                                               (uses generate-root, revokes root at the end)
#   keys.sh with-signer -- CMD                  CMD gets VAULT_ADDR/VAULT_CACERT/VAULT_TOKEN (5 min signer token) in its
#                                               env only (cosign --key hashivault://bravo-supply-chain); revoked after CMD
#   keys.sh signer-pubkey [FILE]                public key of bravo-supply-chain (PEM) -> FILE or stdout (not secret)
#   keys.sh env-fields ENV NAME                   field names of secret/envs/ENV/NAME (no values)
#   keys.sh k8s-auth-setup                        platform-rke2: Kubernetes auth + roles rehearsal, api (root once, revoked)
#   keys.sh env-list [ENV]                        secret names under secret/envs/ (or secret/envs/ENV/), no values
#   keys.sh --target platform-eks CMD          any command against the platform EKS OpenBao (port-forward)
#   keys.sh --target platform-eks tls           platform CA + server cert -> Secret openbao/openbao-tls
#   keys.sh migrate-to-platform [--from T --to T] store -> platform by raft snapshot (shares unchanged)
#   (after the move the default target is platform-eks; BRAVO_BAO_TARGET=pilot reaches the stopped pilot)
#
# Short-lived edge credentials (with-cloudflare, issue-tunnel-token):
#   custody holds an "edge issuer" token that can only mint secret_ids for the edge AppRole;
#   each run mints one secret_id (60 s, 1 use), logs in for a 5 min edge token, reads the secret,
#   revokes the edge token, and passes values only through pipes and the child's environment.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PILOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PILOT_DIR/../.." && pwd)"
ENV_FILE="$PILOT_DIR/.env"
TLS_DIR="$PILOT_DIR/bao/tls"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
CUSTODY="$CUSTODY_DIR/openbao-init.json"
CF_SECRET="platform/cloudflare"
CF_API="https://api.cloudflare.com/client/v4"
ENV_MARK="# CLOUDFLARE_API_TOKEN은 OpenBao secret/platform/cloudflare에 있음(D-26). 사용: scripts/keys.sh with-cloudflare -- <명령>"

die() { printf 'keys.sh: %s\n' "$*" >&2; exit 1; }
log() { printf 'keys.sh: %s\n' "$*"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Value of KEY in .env without sourcing it. Only ever captured with $(...), never printed.
env_get() {
  [[ -f $ENV_FILE ]] || return 0
  local v
  v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)"
  v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  printf '%s' "$v"
}

BAO_PORT="${PLATFORM_BAO_HOST_PORT:-$(env_get PLATFORM_BAO_HOST_PORT)}"
PILOT_BAO_ADDR="https://127.0.0.1:${BAO_PORT:-8200}"
PILOT_TLS_DIR="$TLS_DIR"
BAO_ADDR="${BAO_ADDR:-$PILOT_BAO_ADDR}"

# Target (D-26: one store). pilot = laptop Compose platform-bao. platform-eks = OpenBao on the platform
# EKS (deploy/platform/openbao), reached only through `kubectl port-forward` to pod openbao-0 (no public
# address, the EKS API is open to the operator /32 only). Choose with `keys.sh --target <t> <cmd>` or
# BRAVO_BAO_TARGET. Default: platform-eks once custody records the move (migrate-to-platform), else pilot.
# platform-rke2 = the spec §7.2 platform (EC2 + RKE2 single node, kube API via SSM port forward); platform-eks
# = the interim EKS platform (retired after the move). Context per target: BRAVO_PLATFORM_CONTEXT overrides.
BAO_TARGET="${BRAVO_BAO_TARGET:-}"
if [[ ${1-} == --target ]]; then BAO_TARGET=${2-}; shift 2 || die "usage: keys.sh --target pilot|platform-eks|platform-rke2 <cmd>"
elif [[ ${1-} == --target=* ]]; then BAO_TARGET=${1#--target=}; shift; fi
if [[ -z $BAO_TARGET ]]; then
  BAO_TARGET=pilot
  [[ -f $CUSTODY ]] && BAO_TARGET="$(jq -r '.bao_target // "pilot"' "$CUSTODY" 2>/dev/null || echo pilot)"
fi
target_cfg() { # sets PLATFORM_CONTEXT, PLATFORM_TLS_DIR for a platform target
  case $1 in
    platform-eks) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-bravo-platform}"; PLATFORM_TLS_DIR="$CUSTODY_DIR/platform-eks" ;;
    platform-rke2) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"; PLATFORM_TLS_DIR="$CUSTODY_DIR/platform-rke2" ;;
    pilot) PLATFORM_CONTEXT=""; PLATFORM_TLS_DIR="" ;;
    *) die "unknown target: $1 (pilot | platform-eks | platform-rke2)" ;;
  esac
}
target_cfg "$BAO_TARGET"
PF_PID=""
pf_stop() { [[ -z $PF_PID ]] || { kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; PF_PID=""; }; }
# Start a private port-forward to the platform OpenBao on a free local port for this run only.
pf_start() {
  [[ -z $PF_PID ]] || return 0
  command -v kubectl >/dev/null || die "--target platform-eks needs kubectl"
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
    kill -0 "$PF_PID" 2>/dev/null || die "kubectl port-forward to $PLATFORM_CONTEXT openbao/openbao-0 exited (for platform-rke2 the SSM port forward to the node API must be running)"
    sleep 0.25
  done
  die "platform OpenBao not reachable through port-forward ($PLATFORM_CONTEXT openbao/openbao-0)"
}
use_pilot() { pf_stop; TLS_DIR="$PILOT_TLS_DIR"; BAO_ADDR="$PILOT_BAO_ADDR"; }

tok_hdr() { [[ -z ${1-} ]] || printf 'X-Vault-Token: %s\n' "$1"; }

# api [-d|-p] METHOD PATH [TOKEN]. -d: JSON body on stdin, -p: merge-patch body on stdin.
# The token goes to curl through a pipe (-H @fd), never through argv.
api() {
  local ctype=""
  case $1 in -d) ctype=application/json; shift ;; -p) ctype=application/merge-patch+json; shift ;; esac
  local method=$1 path=$2 token=${3-} out code
  if [[ -n $ctype ]]; then
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -H "Content-Type: $ctype" --data-binary @- -w '\n%{http_code}' "$BAO_ADDR/v1/$path")" \
      || die "cannot reach OpenBao at $BAO_ADDR"
  else
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -w '\n%{http_code}' "$BAO_ADDR/v1/$path" </dev/null)" || die "cannot reach OpenBao at $BAO_ADDR"
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  if [[ $code != 2* ]]; then
    printf 'keys.sh: %s %s -> HTTP %s %s\n' "$method" "$path" "$code" \
      "$(printf '%s' "$out" | jq -c '.errors? // empty' 2>/dev/null || true)" >&2
    return 1
  fi
  printf '%s' "$out"
}

seal_state() { api GET sys/seal-status; }
jfield() { printf '%s' "$1" | jq -r "$2"; }

need_unsealed() {
  local st; st="$(seal_state)"
  [[ $(jfield "$st" .initialized) == true ]] || die "OpenBao is not initialised: scripts/keys.sh init"
  [[ $(jfield "$st" .sealed) == false ]] || die "OpenBao is sealed: scripts/keys.sh unseal"
}

file_mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }

need_custody() {
  [[ -f $CUSTODY ]] || die "no custody file at $CUSTODY (set BRAVO_KEY_CUSTODY_DIR?)"
  [[ $(file_mode "$CUSTODY") == 600 ]] || die "custody file must be mode 600: $CUSTODY"
  [[ $(file_mode "$CUSTODY_DIR") == 700 ]] || die "custody dir must be mode 700: $CUSTODY_DIR"
}

custody_get() { jq -er "$1" "$CUSTODY"; }

# Merge the JSON object on stdin into the custody file (atomic, mode 600).
custody_merge() {
  local tmp; tmp="$(mktemp "$CUSTODY_DIR/.custody.XXXXXX")"
  if jq -s '.[0] * .[1]' "$CUSTODY" - >"$tmp"; then mv -f "$tmp" "$CUSTODY"; else rm -f "$tmp"; die "custody update failed"; fi
}

wait_active() {
  local i code=""
  for i in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$TLS_DIR/ca.crt" --max-time 5 "$BAO_ADDR/v1/sys/health" || true)"
    [[ $code == 200 ]] && return 0
    sleep 1
  done
  die "OpenBao is not active (sys/health HTTP $code)"
}

cmd_tls() {
  if [[ -f $TLS_DIR/server.crt && ${1-} != --rotate ]]; then
    log "TLS files exist in $TLS_DIR (use --rotate to replace, then restart platform-bao)"; return 0
  fi
  mkdir -p "$TLS_DIR"; chmod 700 "$TLS_DIR"
  local d="$TLS_DIR"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 90 \
    -subj "/CN=bravo-pilot platform-bao CA" -keyout "$d/ca.key" -out "$d/ca.crt" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -subj "/CN=platform-bao" -keyout "$d/server.key" -out "$d/server.csr" 2>/dev/null
  openssl x509 -req -in "$d/server.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -days 30 \
    -set_serial "0x$(openssl rand -hex 16)" -out "$d/server.crt" -extfile <(printf '%s\n' \
      "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
      "extendedKeyUsage=serverAuth" "subjectAltName=DNS:platform-bao,DNS:localhost,IP:127.0.0.1") 2>/dev/null
  # The CA key is not kept: rotation issues a new CA. Clients read ca.crt from this directory.
  rm -f "$d/ca.key" "$d/server.csr"
  chmod 600 "$d"/*
  log "TLS: $d/{ca.crt,server.crt,server.key} (mode 600, server cert 30 days)"
}

POLICY_EDGE='# Edge step (D-26): read the platform Cloudflare credentials, write per-environment cf-* secrets.
path "secret/data/platform/cloudflare" { capabilities = ["read"] }
path "secret/data/envs/+/cf-*" { capabilities = ["create", "update"] }'

POLICY_EDGE_ISSUER='# Mints short-lived secret_ids for the edge AppRole only (keys.sh with-cloudflare).
path "auth/approle/role/edge/role-id" { capabilities = ["read"] }
path "auth/approle/role/edge/secret-id" { capabilities = ["update"] }'

POLICY_ADMIN='# Day-to-day platform administration. No seal, root generation, mounts or auth-method changes.
path "secret/*" { capabilities = ["create", "read", "update", "patch", "delete", "list"] }
path "sys/policies/acl" { capabilities = ["list"] }
path "sys/policies/acl/*" { capabilities = ["create", "read", "update", "delete", "list"] }
path "auth/approle/role" { capabilities = ["list"] }
path "auth/approle/role/*" { capabilities = ["create", "read", "update", "delete", "list"] }
path "sys/mounts" { capabilities = ["read"] }
path "sys/auth" { capabilities = ["read"] }
path "sys/audit" { capabilities = ["read", "sudo"] }
path "auth/token/create" { capabilities = ["update"] }
path "auth/token/revoke-accessor" { capabilities = ["update"] }
# Root regeneration needs this token AND a quorum of shares (keys.sh generate-root).
path "sys/generate-root-token/*" { capabilities = ["create", "read", "update", "delete", "sudo"] }'

put_policy() { jq -n --arg p "$2" '{policy: $p}' | api -d PUT "sys/policies/acl/$1" "$3" >/dev/null; log "policy $1"; }

cmd_init() {
  [[ -f $TLS_DIR/ca.crt ]] || die "no TLS files: scripts/keys.sh tls"
  local st; st="$(seal_state)"
  [[ $(jfield "$st" .initialized) == false ]] || die "OpenBao is already initialised"
  [[ ! -e $CUSTODY ]] || die "custody file already exists, refusing to overwrite: $CUSTODY"
  mkdir -p "$CUSTODY_DIR"; chmod 700 "$CUSTODY_DIR"

  # The init response (shares + root token) goes straight into the custody file.
  local tmp; tmp="$(mktemp "$CUSTODY_DIR/.init.XXXXXX")"
  printf '{"secret_shares":3,"secret_threshold":2}' | api -d PUT sys/init >"$tmp"
  mv -f "$tmp" "$CUSTODY"; chmod 600 "$CUSTODY"
  jq -e '(.keys_base64 | length) == 3 and (.root_token | length) > 0' "$CUSTODY" >/dev/null \
    || die "init response incomplete; inspect $CUSTODY"
  jq -n --arg at "$(now)" --arg addr "$BAO_ADDR" \
    '{created_at: $at, bao_addr: $addr, shares: 3, threshold: 2, root_token_revoked_at: null}' | custody_merge
  log "initialised: Shamir 3 shares / threshold 2 -> $CUSTODY (mode 600, dir 700)"

  cmd_unseal
  local root; root="$(custody_get .root_token)"

  if api GET sys/mounts "$root" | jq -e '(.data // .) | has("secret/")' >/dev/null; then
    log "kv: secret/ exists"
  else
    printf '{"type":"kv","options":{"version":"2"},"description":"platform secrets (D-26)"}' \
      | api -d POST sys/mounts/secret "$root" >/dev/null
    log "kv: v2 enabled at secret/"
  fi

  put_policy platform-edge "$POLICY_EDGE" "$root"
  put_policy platform-edge-issuer "$POLICY_EDGE_ISSUER" "$root"
  put_policy platform-admin "$POLICY_ADMIN" "$root"

  if ! api GET sys/auth "$root" | jq -e '(.data // .) | has("approle/")' >/dev/null; then
    printf '{"type":"approle","description":"platform services (D-26)"}' | api -d POST sys/auth/approle "$root" >/dev/null
  fi
  printf '%s' '{"token_policies":["platform-edge"],"token_type":"service","token_ttl":"5m","token_max_ttl":"10m",
    "bind_secret_id":true,"secret_id_ttl":"60s","secret_id_num_uses":1}' \
    | api -d POST auth/approle/role/edge "$root" >/dev/null
  log "approle: role edge (policy platform-edge, token 5m, secret_id 60s / 1 use)"

  if api GET sys/audit "$root" | jq -e '(.data // .) | has("file/")' >/dev/null; then
    log "audit: file device on (/openbao/file/audit.log, declared in bao/bao.hcl)"
  else
    printf '{"type":"file","options":{"file_path":"/openbao/file/audit.log"}}' \
      | api -d PUT sys/audit/file "$root" >/dev/null
    log "audit: file device enabled"
  fi

  # Orphan periodic operator tokens survive the root revocation. They go only to custody.
  printf '{"policies":["platform-admin"],"period":"168h","display_name":"operator-admin"}' \
    | api -d POST auth/token/create-orphan "$root" \
    | jq '{admin_token: .auth.client_token, admin_token_accessor: .auth.accessor}' | custody_merge
  printf '{"policies":["platform-edge-issuer"],"period":"168h","display_name":"edge-issuer"}' \
    | api -d POST auth/token/create-orphan "$root" \
    | jq '{edge_issuer_token: .auth.client_token, edge_issuer_token_accessor: .auth.accessor}' | custody_merge
  log "operator tokens: platform-admin, platform-edge-issuer (periodic 168h) -> custody"

  api POST auth/token/revoke-self "$root" >/dev/null
  jq -n --arg at "$(now)" '{root_token_revoked_at: $at}' | custody_merge
  log "root token revoked. Regenerate with: scripts/keys.sh generate-root (uses the shares)"
}

cmd_unseal() {
  local st; st="$(seal_state)"
  [[ $(jfield "$st" .initialized) == true ]] || die "OpenBao is not initialised: scripts/keys.sh init"
  if [[ $(jfield "$st" .sealed) == false ]]; then log "already unsealed"; return 0; fi
  need_custody
  local i n; n="$(custody_get '.keys_base64 | length')"
  for ((i = 0; i < n; i++)); do
    st="$(jq -c --argjson i "$i" '{key: .keys_base64[$i]}' "$CUSTODY" | api -d PUT sys/unseal)"
    [[ $(jfield "$st" .sealed) == false ]] && break
  done
  [[ $(jfield "$st" .sealed) == false ]] || die "still sealed after $n shares"
  wait_active
  log "unsealed ($(jfield "$st" .t) of $(jfield "$st" .n) shares)"
}

cmd_status() {
  local st; st="$(seal_state)"
  jfield "$st" '"addr='"$BAO_ADDR"' version=\(.version) initialized=\(.initialized) sealed=\(.sealed) shares=\(.n) threshold=\(.t) storage=\(.storage_type // "?")"'
  if [[ -f $CUSTODY ]]; then
    printf 'custody: %s (mode %s), root token revoked at %s\n' "$CUSTODY" "$(file_mode "$CUSTODY")" \
      "$(jq -r '.root_token_revoked_at // "NOT REVOKED"' "$CUSTODY")"
  else
    printf 'custody: none at %s\n' "$CUSTODY"
  fi
  [[ $(jfield "$st" .sealed) == false && -f $CUSTODY ]] || return 0
  local admin; admin="$(custody_get .admin_token)"
  printf 'audit devices: %s\n' "$(api GET sys/audit "$admin" | jq -c '[(.data // .) | to_entries[] | select(.value | type == "object") | "\(.key) \(.value.type) \(.value.options.file_path // "")"]')"
  local md
  if md="$(api GET "secret/metadata/$CF_SECRET" "$admin" 2>/dev/null)"; then
    printf 'secret/%s: version %s, updated %s, fields %s\n' "$CF_SECRET" "$(jfield "$md" .data.current_version)" \
      "$(jfield "$md" .data.updated_time)" \
      "$(api GET "secret/data/$CF_SECRET" "$admin" | jq -c '.data.data | to_entries | map("\(.key)=\(if .value == "" then "empty" else "set" end)")')"
  else
    printf 'secret/%s: not written\n' "$CF_SECRET"
  fi
}

# Replace the CLOUDFLARE_API_TOKEN line in .env with an empty value and a pointer comment.
blank_env_token() {
  local tmp; tmp="$(mktemp "$PILOT_DIR/.env.XXXXXX")"
  awk -v mark="$ENV_MARK" '
    $0 == mark { next }
    /^CLOUDFLARE_API_TOKEN=/ { print mark; print "CLOUDFLARE_API_TOKEN="; next }
    { print }' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"; mv -f "$tmp" "$ENV_FILE"
}

cmd_import_cloudflare() {
  [[ -f $ENV_FILE ]] || die "no $ENV_FILE"
  need_custody; need_unsealed
  local admin tok acct zone got
  admin="$(custody_get .admin_token)"
  tok="$(env_get CLOUDFLARE_API_TOKEN)"; acct="$(env_get CLOUDFLARE_ACCOUNT_ID)"; zone="$(env_get CLOUDFLARE_ZONE_ID)"

  if [[ -z $tok ]]; then
    # Token already moved: patch only the ids that are set in .env.
    api GET "secret/metadata/$CF_SECRET" "$admin" >/dev/null 2>&1 \
      || die "CLOUDFLARE_API_TOKEN is empty in .env and secret/$CF_SECRET does not exist"
    printf '%s\n%s\n' "$acct" "$zone" \
      | jq -Rn '[inputs] as $v | {data: ({account_id: $v[0], zone_id: $v[1]} | with_entries(select(.value != "")))}' \
      | api -p PATCH "secret/data/$CF_SECRET" "$admin" >/dev/null
    got="$(api GET "secret/data/$CF_SECRET" "$admin")"
    [[ -z $acct || $(jfield "$got" .data.data.account_id) == "$acct" ]] || die "account_id read-back mismatch"
    [[ -z $zone || $(jfield "$got" .data.data.zone_id) == "$zone" ]] || die "zone_id read-back mismatch"
    log "token already in OpenBao; patched non-empty ids from .env (read-back ok)"
    return 0
  fi

  printf '%s\n%s\n%s\n' "$tok" "$acct" "$zone" \
    | jq -Rn '[inputs] as $v | {data: {api_token: $v[0], account_id: $v[1], zone_id: $v[2]}}' \
    | api -d POST "secret/data/$CF_SECRET" "$admin" \
    | jq -r '"written secret/'"$CF_SECRET"' version \(.data.version)"'
  got="$(api GET "secret/data/$CF_SECRET" "$admin")"
  [[ $(jfield "$got" .data.data.api_token) == "$tok" ]] || die "api_token read-back mismatch; .env left unchanged"
  [[ $(jfield "$got" .data.data.account_id) == "$acct" ]] || die "account_id read-back mismatch; .env left unchanged"
  [[ $(jfield "$got" .data.data.zone_id) == "$zone" ]] || die "zone_id read-back mismatch; .env left unchanged"
  log "read-back ok: api_token, account_id, zone_id match (values not shown)"

  # Other copies of the token in the repository (file names only).
  local copies
  copies="$(grep -rlF --exclude=.env --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
    --exclude-dir=.terraform -f <(printf '%s\n' "$tok") "$REPO_ROOT" 2>/dev/null || true)"
  [[ -z $copies ]] || log "WARNING: the token also appears in: $copies"

  blank_env_token
  [[ -z $(env_get CLOUDFLARE_API_TOKEN) ]] || die "could not blank CLOUDFLARE_API_TOKEN in .env"
  log "CLOUDFLARE_API_TOKEN blanked in .env (comment points to OpenBao)"
  [[ -n $zone ]] || log "note: CLOUDFLARE_ZONE_ID is empty. Set it in .env and run import-cloudflare again (patches ids only)."
}

# Sets EDGE_TOKEN (not exported): one fresh secret_id (60 s, 1 use) -> 5 min edge token.
edge_login() {
  need_custody; need_unsealed
  local issuer role_id secret_id
  issuer="$(custody_get .edge_issuer_token)"
  api POST auth/token/renew-self "$issuer" >/dev/null \
    || die "edge issuer token invalid or expired (README: regenerate root, then reissue)"
  role_id="$(api GET auth/approle/role/edge/role-id "$issuer" | jq -er .data.role_id)"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST auth/approle/role/edge/secret-id "$issuer" | jq -er .data.secret_id)"
  EDGE_TOKEN="$(printf '%s\n%s\n' "$role_id" "$secret_id" \
    | jq -Rn '[inputs] as $v | {role_id: $v[0], secret_id: $v[1]}' \
    | api -d POST auth/approle/login | jq -er .auth.client_token)"
}
edge_logout() { api POST auth/token/revoke-self "$EDGE_TOKEN" >/dev/null || true; EDGE_TOKEN=""; }

# Sets CF_TOKEN, CF_ACCOUNT, CF_ZONE (not exported).
cf_load() {
  local j; j="$(api GET "secret/data/$CF_SECRET" "$EDGE_TOKEN")" || die "cannot read secret/$CF_SECRET as edge"
  CF_TOKEN="$(jfield "$j" '.data.data.api_token // ""')"
  CF_ACCOUNT="$(jfield "$j" '.data.data.account_id // ""')"
  CF_ZONE="$(jfield "$j" '.data.data.zone_id // ""')"
  [[ -n $CF_TOKEN ]] || die "secret/$CF_SECRET has no api_token: scripts/keys.sh import-cloudflare"
}

cmd_with_cloudflare() {
  [[ ${1-} == -- ]] && shift
  (($#)) || die "usage: keys.sh with-cloudflare -- <cmd> [args...]"
  edge_login; cf_load; edge_logout
  pf_stop
  CLOUDFLARE_API_TOKEN="$CF_TOKEN" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT" CLOUDFLARE_ZONE_ID="$CF_ZONE" exec "$@"
}

# keys.sh issue-tunnel-token ENV TUNNEL_ID [NAME]: NAME defaults to cf-tunnel (user entry tunnel); the D-16
# management tunnels use cf-mgmt-tunnel. NAME must start with cf- (edge policy: secret/data/envs/+/cf-*).
cmd_issue_tunnel_token() {
  local env=${1-} tid=${2-} name=${3:-cf-tunnel}
  [[ $env =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "usage: keys.sh issue-tunnel-token <env> <tunnel_id> [cf-name] (env: a-z 0-9 -)"
  [[ $name =~ ^cf-[a-z0-9-]{1,60}$ ]] || die "secret name must start with cf- (a-z 0-9 -)"
  [[ $tid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "tunnel_id must be a UUID"
  edge_login; cf_load
  [[ -n $CF_ACCOUNT ]] || { edge_logout; die "secret/$CF_SECRET has no account_id"; }
  # URL and Authorization header go to curl as a config on stdin, not argv.
  local resp
  resp="$(printf 'url = "%s/accounts/%s/cfd_tunnel/%s/token"\nheader = "Authorization: Bearer %s"\n' \
    "$CF_API" "$CF_ACCOUNT" "$tid" "$CF_TOKEN" | curl -sS --max-time 30 -K -)" || { edge_logout; die "Cloudflare API unreachable"; }
  if ! printf '%s' "$resp" | jq -e '.success == true and (.result | type == "string") and (.result | length > 0)' >/dev/null 2>&1; then
    printf '%s' "$resp" | jq -c '{success, errors: [.errors[]? | {code, message}]}' >&2 2>/dev/null || true
    edge_logout; die "Cloudflare did not return a tunnel token"
  fi
  printf '%s' "$resp" | jq '{data: {token: .result}}' \
    | api -d POST "secret/data/envs/$env/$name" "$EDGE_TOKEN" \
    | jq -r '"written secret/envs/'"$env/$name"' version \(.data.version) (field token, value not shown)"'
  edge_logout
}

# keys.sh issue-service-token ENV TOKEN_NAME [SECRET_NAME] [DURATION]
# New Cloudflare Access service token -> secret/envs/ENV/SECRET_NAME, fields client_id, client_secret,
# token_id, name, expires_at (D-16, D-26). SECRET_NAME defaults to cf-access-service-token and must start
# with cf- (edge policy). DURATION defaults to 168h (02-network.md 4.3: 7 days). Prints only the token ID,
# which Terraform takes as input (link_service_token_ids). Never re-issues: if the OpenBao secret or a
# Cloudflare token with the same name exists, it stops; rotation is a separate, deliberate step.
cf_call() { # METHOD PATH: JSON body (if any) on stdin; URL and Authorization go to curl as config, not argv
  local m=$1 p=$2
  if [[ $m == GET || $m == DELETE ]]; then
    curl -sS --max-time 30 -X "$m" -K <(printf 'url = "%s%s"\nheader = "Authorization: Bearer %s"\n' "$CF_API" "$p" "$CF_TOKEN") </dev/null
  else
    curl -sS --max-time 30 -X "$m" -H 'Content-Type: application/json' --data-binary @- \
      -K <(printf 'url = "%s%s"\nheader = "Authorization: Bearer %s"\n' "$CF_API" "$p" "$CF_TOKEN")
  fi
}
cf_errors() { jq -c '[.errors[]? | {code, message}]' 2>/dev/null || printf 'unparseable response'; }

cmd_issue_service_token() {
  local env=${1-} tname=${2-} sname=${3:-cf-access-service-token} dur=${4:-168h} admin resp tid
  [[ $env =~ ^[a-z0-9][a-z0-9-]{0,62}$ && $tname =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] \
    || die "usage: keys.sh issue-service-token <env> <token_name> [secret_name] [duration] (a-z 0-9 -)"
  [[ $sname =~ ^cf-[a-z0-9-]{1,60}$ ]] || die "secret_name must start with cf- (edge policy secret/envs/+/cf-*)"
  [[ $dur =~ ^[1-9][0-9]*h$ ]] || die "duration must look like 168h"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  if api GET "secret/metadata/envs/$env/$sname" "$admin" >/dev/null 2>&1; then
    die "secret/envs/$env/$sname already exists; not re-issuing (rotate deliberately)"
  fi
  edge_login; cf_load
  [[ -n $CF_ACCOUNT ]] || { edge_logout; die "secret/$CF_SECRET has no account_id"; }

  resp="$(cf_call GET "/accounts/$CF_ACCOUNT/access/service_tokens?name=$tname")" || { edge_logout; die "Cloudflare API unreachable"; }
  if ! jq -e '.success == true' <<<"$resp" >/dev/null 2>&1; then
    printf 'keys.sh: Cloudflare: %s\n' "$(cf_errors <<<"$resp")" >&2; edge_logout; die "cannot list Access service tokens"
  fi
  tid="$(jq -r --arg n "$tname" '[.result[]? | select(.name == $n) | .id][0] // ""' <<<"$resp")"
  [[ -z $tid ]] || { edge_logout; die "Cloudflare already has service token $tname (id $tid) but OpenBao does not; its secret cannot be read back. Delete or rotate it first"; }

  resp="$(jq -n --arg n "$tname" --arg d "$dur" '{name: $n, duration: $d}' \
    | cf_call POST "/accounts/$CF_ACCOUNT/access/service_tokens")" || { edge_logout; die "Cloudflare API unreachable"; }
  if ! jq -e '.success == true and (.result.client_secret // "" | length > 0)' <<<"$resp" >/dev/null 2>&1; then
    printf 'keys.sh: Cloudflare: %s\n' "$(cf_errors <<<"$resp")" >&2; edge_logout; die "Cloudflare did not return a service token"
  fi
  tid="$(jq -r .result.id <<<"$resp")"
  if ! jq '{data: {client_id: .result.client_id, client_secret: .result.client_secret, token_id: .result.id,
        name: .result.name, expires_at: .result.expires_at}}' <<<"$resp" \
      | api -d POST "secret/data/envs/$env/$sname" "$EDGE_TOKEN" >/dev/null; then
    # Do not leave a token whose secret nobody holds.
    cf_call DELETE "/accounts/$CF_ACCOUNT/access/service_tokens/$tid" >/dev/null || true
    resp=""; edge_logout; die "OpenBao write failed; deleted the new Cloudflare token $tid"
  fi
  log "service token $tname id $tid (expires $(jq -r .result.expires_at <<<"$resp")) -> secret/envs/$env/$sname (client_id, client_secret not shown)"
  resp=""
  edge_logout
}

# A new root token from the shares, written to custody only. OpenBao 2.7 disables the
# unauthenticated sys/generate-root endpoints, so this uses sys/generate-root-token with the
# platform-admin token from custody plus the shares.
cmd_generate_root() {
  need_custody; need_unsealed
  local admin att nonce otp i n st enc root
  admin="$(custody_get .admin_token)"
  api DELETE sys/generate-root-token/attempt "$admin" >/dev/null 2>&1 || true # clear a stale attempt
  att="$(api POST sys/generate-root-token/attempt "$admin")"
  att="$(jfield "$att" "(.data // .)")"; nonce="$(jfield "$att" .nonce)"; otp="$(jfield "$att" .otp)"
  n="$(custody_get '.keys_base64 | length')"
  for ((i = 0; i < n; i++)); do
    st="$(jq -c --argjson i "$i" --arg nonce "$nonce" '{key: .keys_base64[$i], nonce: $nonce}' "$CUSTODY" \
      | api -d POST sys/generate-root-token/update "$admin" | jq -c "(.data // .)")"
    [[ $(jfield "$st" .complete) == true ]] && break
  done
  [[ $(jfield "$st" .complete) == true ]] || die "generate-root did not complete"
  enc="$(jfield "$st" '.encoded_token // .encoded_root_token')"
  root="$(printf '%s\n%s\n' "$enc" "$otp" | python3 -I -c '
import base64, sys
enc, otp = sys.stdin.read().split("\n")[:2]
raw = base64.b64decode(enc + "=" * (-len(enc) % 4))
sys.stdout.write(bytes(a ^ b for a, b in zip(raw, otp.encode())).decode())')"
  api GET auth/token/lookup-self "$root" >/dev/null || die "decoded root token does not work"
  printf '%s' "$root" | jq -Rs --arg at "$(now)" '{root_token: ., root_token_generated_at: $at, root_token_revoked_at: null}' | custody_merge
  log "new root token written to custody only ($CUSTODY). Revoke when done: scripts/keys.sh revoke-root"
}

cmd_revoke_root() {
  need_custody; need_unsealed
  [[ $(jq -r '.root_token_revoked_at // ""' "$CUSTODY") == "" ]] || { log "root token already revoked"; return 0; }
  api POST auth/token/revoke-self "$(custody_get .root_token)" >/dev/null
  jq -n --arg at "$(now)" '{root_token_revoked_at: $at}' | custody_merge
  log "root token revoked"
}

# Per-environment secrets for a cluster (CLOUD-E2E-CONTRACT §4, §8). Values never reach argv,
# files or the terminal: generated with openssl into a pipe, delivered on a child's stdin.
POLICY_ENV_DELIVER='# Reads one environment secret for a single delivery run (keys.sh env-pipe).
path "secret/data/envs/+/*" { capabilities = ["read"] }'

env_path_check() {
  [[ $1 =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "env must match a-z 0-9 -"
  [[ $2 =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "secret name must match a-z 0-9 -"
  [[ $3 =~ ^[a-z0-9_]{1,64}$ ]] || die "field must match a-z 0-9 _"
}

# keys.sh env-generate <env> <name> <field> [bytes]: random hex value, only if the field is absent.
cmd_env_generate() {
  local env=${1-} name=${2-} field=${3-} bytes=${4:-32} admin cur
  env_path_check "$env" "$name" "$field"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  if cur="$(api GET "secret/data/envs/$env/$name" "$admin" 2>/dev/null)"; then
    if jq -e --arg f "$field" '.data.data[$f] // "" | length > 0' <<<"$cur" >/dev/null; then
      log "secret/envs/$env/$name field $field exists (version $(jfield "$cur" .data.metadata.version)); not changed"
      return 0
    fi
    openssl rand -hex "$bytes" | jq -Rn --arg f "$field" '{data: {($f): input}}' \
      | api -p PATCH "secret/data/envs/$env/$name" "$admin" >/dev/null
  else
    openssl rand -hex "$bytes" | jq -Rn --arg f "$field" '{data: {($f): input}}' \
      | api -d POST "secret/data/envs/$env/$name" "$admin" >/dev/null
  fi
  log "generated secret/envs/$env/$name field $field ($bytes random bytes, hex; value not shown)"
}

# keys.sh env-put <env> <name> <field>: store a value that something else generated (e.g. the CNPG app
# password on the source cluster), read from stdin only. Same value already there: no change. A
# different value already there: refused (no silent overwrite). Prints the KV version, never the value.
cmd_env_put() {
  local env=${1-} name=${2-} field=${3-} admin cur val
  env_path_check "$env" "$name" "$field"
  [[ ! -t 0 ]] || die "env-put reads the value from stdin (pipe it in)"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  val="$(cat)"
  [[ -n $val ]] || die "env-put: empty value on stdin"
  if cur="$(api GET "secret/data/envs/$env/$name" "$admin" 2>/dev/null)"; then
    if jq -e --arg f "$field" '.data.data | has($f)' <<<"$cur" >/dev/null; then
      if printf '%s' "$val" | jq -Rse --arg f "$field" --argjson cur "$cur" '. == $cur.data.data[$f]' >/dev/null; then
        log "secret/envs/$env/$name field $field already holds this value (version $(jfield "$cur" .data.metadata.version)); not changed"
        return 0
      fi
      die "secret/envs/$env/$name field $field holds a different value; refusing to overwrite"
    fi
    printf '%s' "$val" | jq -Rs --arg f "$field" '{data: {($f): .}}' \
      | api -p PATCH "secret/data/envs/$env/$name" "$admin" >/dev/null
  else
    printf '%s' "$val" | jq -Rs --arg f "$field" '{data: {($f): .}}' \
      | api -d POST "secret/data/envs/$env/$name" "$admin" >/dev/null
  fi
  val=""
  log "stored secret/envs/$env/$name field $field from stdin (value not shown)"
}

# keys.sh env-delete <env> <name>: remove a wrongly stored secret (all versions). Value never shown.
cmd_env_delete() {
  local env=${1-} name=${2-} admin
  env_path_check "$env" "$name" x
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  api DELETE "secret/metadata/envs/$env/$name" "$admin" >/dev/null || die "delete failed for secret/envs/$env/$name"
  log "deleted secret/envs/$env/$name (all versions)"
}

# keys.sh env-copy <src-env> <name> <field> <dst-env>: copy one field between environments inside
# OpenBao (e.g. app_writer must equal the source value while the destination PgBouncer points at the
# source DB). The value moves GET -> jq -> PATCH on pipes only; it replaces the destination field and
# is checked by comparing both reads inside jq. Prints versions, never the value.
cmd_env_copy() {
  local src=${1-} name=${2-} field=${3-} dst=${4-} admin before same
  env_path_check "$src" "$name" "$field"; env_path_check "$dst" "$name" "$field"
  [[ $src != "$dst" ]] || die "env-copy: source and destination are the same"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  api GET "secret/data/envs/$src/$name" "$admin" | jq -e --arg f "$field" '.data.data[$f] // "" | length > 0' >/dev/null \
    || die "secret/envs/$src/$name has no field $field"
  if before="$(api GET "secret/data/envs/$dst/$name" "$admin" 2>/dev/null | jq -r '.data.metadata.version')"; then
    api GET "secret/data/envs/$src/$name" "$admin" | jq --arg f "$field" '{data: {($f): .data.data[$f]}}' \
      | api -p PATCH "secret/data/envs/$dst/$name" "$admin" >/dev/null
  else
    before=none
    api GET "secret/data/envs/$src/$name" "$admin" | jq --arg f "$field" '{data: {($f): .data.data[$f]}}' \
      | api -d POST "secret/data/envs/$dst/$name" "$admin" >/dev/null
  fi
  same="$({ api GET "secret/data/envs/$src/$name" "$admin"; api GET "secret/data/envs/$dst/$name" "$admin"; } \
    | jq -s --arg f "$field" '.[0].data.data[$f] == .[1].data.data[$f]')"
  [[ $same == true ]] || die "env-copy: read-back differs"
  log "copied secret/envs/$src/$name field $field -> secret/envs/$dst/$name (version $before -> $(api GET "secret/data/envs/$dst/$name" "$admin" | jq -r .data.metadata.version)); read-back equal (value not shown)"
}

# keys.sh env-pipe <env> <name> <field> -- CMD...: CMD gets the value on stdin (no newline).
# The read uses a one-use secret_id (60 s) for AppRole env-deliver, then revokes that token.
cmd_env_pipe() {
  local env=${1-} name=${2-} field=${3-} admin role_id secret_id tok j
  env_path_check "$env" "$name" "$field"; shift 3
  [[ ${1-} == -- ]] && shift
  (($#)) || die "usage: keys.sh env-pipe <env> <name> <field> -- <cmd> [args...]"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  if ! api GET sys/policies/acl/platform-env-deliver "$admin" >/dev/null 2>&1; then
    # Logs go to stderr: stdout of this command is the value pipe.
    put_policy platform-env-deliver "$POLICY_ENV_DELIVER" "$admin" >&2
    printf '%s' '{"token_policies":["platform-env-deliver"],"token_type":"service","token_ttl":"2m",
      "token_max_ttl":"5m","bind_secret_id":true,"secret_id_ttl":"60s","secret_id_num_uses":1}' \
      | api -d POST auth/approle/role/env-deliver "$admin" >/dev/null
    log "approle: role env-deliver (policy platform-env-deliver, token 2m, secret_id 60s / 1 use)" >&2
  fi
  role_id="$(api GET auth/approle/role/env-deliver/role-id "$admin" | jq -er .data.role_id)"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST auth/approle/role/env-deliver/secret-id "$admin" | jq -er .data.secret_id)"
  tok="$(printf '%s\n%s\n' "$role_id" "$secret_id" | jq -Rn '[inputs] as $v | {role_id: $v[0], secret_id: $v[1]}' \
    | api -d POST auth/approle/login | jq -er .auth.client_token)"
  j="$(api GET "secret/data/envs/$env/$name" "$tok")" || { api POST auth/token/revoke-self "$tok" >/dev/null || true; die "cannot read secret/envs/$env/$name"; }
  api POST auth/token/revoke-self "$tok" >/dev/null || true
  jq -e --arg f "$field" '.data.data[$f] // "" | length > 0' <<<"$j" >/dev/null || die "secret/envs/$env/$name has no field $field"
  jq -j --arg f "$field" '.data.data[$f]' <<<"$j" | "$@"
}

# --- T31 supply-chain signer (OpenBao transit, D-26) -------------------------------------------------------
# The trusted apply pipeline (platform/apply) signs image digests with cosign --key hashivault://KEY. The key is
# created inside OpenBao transit, is not exportable and never leaves it. A signing run gets a 5 min token from a
# one-use secret_id (60 s) minted by the signer issuer token in custody, same model as the edge step.
SIGNER_KEY="bravo-supply-chain"
POLICY_SIGNER='# T31 signer (D-26): sign with the platform supply-chain key and read its public key. Nothing else.
path "transit/sign/bravo-supply-chain" { capabilities = ["update"] }
path "transit/sign/bravo-supply-chain/*" { capabilities = ["update"] }
path "transit/keys/bravo-supply-chain" { capabilities = ["read"] }'

POLICY_SIGNER_ISSUER='# Mints short-lived secret_ids for the signer AppRole only (keys.sh with-signer).
path "auth/approle/role/signer/role-id" { capabilities = ["read"] }
path "auth/approle/role/signer/secret-id" { capabilities = ["update"] }'

# Revoke the root token this run generated (before the port-forward stops), whatever happened.
signer_cleanup() {
  if [[ -f $CUSTODY && $(jq -r '.root_token_revoked_at // ""' "$CUSTODY") == "" ]]; then cmd_revoke_root || true; fi
  pf_stop
}

cmd_signer_init() {
  need_custody; need_unsealed
  [[ $(jq -r '.root_token_revoked_at // ""' "$CUSTODY") != "" ]] \
    || die "a root token is active in custody; revoke it first (keys.sh revoke-root)"
  local root issuer
  # Mounting an engine and creating orphan tokens need root: regenerate it from the shares for this run only.
  trap signer_cleanup EXIT
  cmd_generate_root >/dev/null
  root="$(custody_get .root_token)"
  if api GET sys/mounts "$root" | jq -e '(.data // .) | has("transit/")' >/dev/null; then
    log "transit: mounted at transit/"
  else
    printf '{"type":"transit","description":"platform signing keys (D-26, T31)"}' \
      | api -d POST sys/mounts/transit "$root" >/dev/null
    log "transit: enabled at transit/"
  fi
  if api GET "transit/keys/$SIGNER_KEY" "$root" >/dev/null 2>&1; then
    log "transit key $SIGNER_KEY exists"
  else
    printf '{"type":"ecdsa-p256","exportable":false,"allow_plaintext_backup":false}' \
      | api -d POST "transit/keys/$SIGNER_KEY" "$root" >/dev/null
    log "transit key $SIGNER_KEY created (ecdsa-p256, not exportable, no plaintext backup)"
  fi
  put_policy platform-signer "$POLICY_SIGNER" "$root"
  put_policy platform-signer-issuer "$POLICY_SIGNER_ISSUER" "$root"
  printf '%s' '{"token_policies":["platform-signer"],"token_type":"service","token_ttl":"5m","token_max_ttl":"10m",
    "bind_secret_id":true,"secret_id_ttl":"60s","secret_id_num_uses":1}' \
    | api -d POST auth/approle/role/signer "$root" >/dev/null
  log "approle: role signer (policy platform-signer, token 5m, secret_id 60s / 1 use)"
  issuer="$(jq -r '.signer_issuer_token // ""' "$CUSTODY")"
  if [[ -n $issuer ]] && api GET auth/token/lookup-self "$issuer" >/dev/null 2>&1; then
    log "signer issuer token in custody is valid; kept"
  else
    printf '{"policies":["platform-signer-issuer"],"period":"168h","display_name":"signer-issuer"}' \
      | api -d POST auth/token/create-orphan "$root" \
      | jq '{signer_issuer_token: .auth.client_token, signer_issuer_token_accessor: .auth.accessor}' | custody_merge
    log "signer issuer token (platform-signer-issuer, periodic 168h) -> custody"
  fi
  issuer=""
  cmd_revoke_root
}

# Sets SIGNER_TOKEN (not exported): one fresh secret_id (60 s, 1 use) -> 5 min signer token.
signer_login() {
  need_custody; need_unsealed
  local issuer role_id secret_id
  issuer="$(jq -r '.signer_issuer_token // ""' "$CUSTODY")"
  [[ -n $issuer ]] || die "no signer issuer token in custody: keys.sh signer-init"
  api POST auth/token/renew-self "$issuer" >/dev/null || die "signer issuer token invalid or expired: keys.sh signer-init"
  role_id="$(api GET auth/approle/role/signer/role-id "$issuer" | jq -er .data.role_id)"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST auth/approle/role/signer/secret-id "$issuer" | jq -er .data.secret_id)"
  SIGNER_TOKEN="$(printf '%s\n%s\n' "$role_id" "$secret_id" \
    | jq -Rn '[inputs] as $v | {role_id: $v[0], secret_id: $v[1]}' \
    | api -d POST auth/approle/login | jq -er .auth.client_token)"
}
signer_logout() { api POST auth/token/revoke-self "$SIGNER_TOKEN" >/dev/null || true; SIGNER_TOKEN=""; }

cmd_with_signer() {
  [[ ${1-} == -- ]] && shift
  (($#)) || die "usage: keys.sh with-signer -- <cmd> [args...]"
  signer_login
  local rc=0
  # The token reaches only CMD's environment (cosign's hashivault KMS reads VAULT_*), never argv or a file.
  VAULT_ADDR="$BAO_ADDR" VAULT_CACERT="$TLS_DIR/ca.crt" VAULT_TOKEN="$SIGNER_TOKEN" \
    TRANSIT_SECRET_ENGINE_PATH=transit "$@" || rc=$?
  signer_logout
  return "$rc"
}

cmd_signer_pubkey() {
  local out=${1-} j pem ver
  signer_login
  j="$(api GET "transit/keys/$SIGNER_KEY" "$SIGNER_TOKEN")" || { signer_logout; die "cannot read transit/keys/$SIGNER_KEY"; }
  signer_logout
  ver="$(jq -r .data.latest_version <<<"$j")"
  pem="$(jq -r --arg v "$ver" '.data.keys[$v].public_key // ""' <<<"$j")"
  [[ $pem == "-----BEGIN PUBLIC KEY-----"* ]] || die "transit key $SIGNER_KEY has no public key for version $ver"
  if [[ -n $out ]]; then
    printf '%s\n' "$pem" >"$out"; chmod 644 "$out"
    log "public key $SIGNER_KEY v$ver -> $out (sha256 $(openssl dgst -sha256 -r "$out" | cut -d' ' -f1))"
  else
    printf '%s\n' "$pem"
  fi
}

# Model credentials for the agent worker (13 6, D-26). claude: the lead's `claude setup-token` OAuth token
# (secret/envs/platform/llm-claude oauth_token, stored with env-put). openai: the OpenAI API key
# (secret/envs/platform/llm-openai api_key). The policy files allow reading that one path only.
LLM_POLICY_DIR="$PILOT_DIR/bao/policies"
# openai also gets platform-llm-openai-refresh (update on the same path): the worker writes a refreshed ChatGPT
# session back with check-and-set (bravo_agents.llm_secret). claude stays read-only.
llm_role_setup() { # PROVIDER ADMIN_TOKEN: policies platform-llm-<p>[-refresh] + AppRole llm-<p> (idempotent; logs on stderr)
  local p=$1 admin=$2 pol="platform-llm-$1" role="llm-$1" pols f
  [[ -f $LLM_POLICY_DIR/$pol.hcl ]] || die "missing $LLM_POLICY_DIR/$pol.hcl"
  pols="$pol"
  [[ -f $LLM_POLICY_DIR/$pol-refresh.hcl ]] && pols="$pol,$pol-refresh"
  for f in ${pols//,/ }; do
    jq -Rs '{policy: .}' "$LLM_POLICY_DIR/$f.hcl" | api -d PUT "sys/policies/acl/$f" "$admin" >/dev/null
  done
  jq -n --arg p "$pols" '{token_policies: ($p | split(",")), token_type: "service", token_ttl: "2m",
      token_max_ttl: "5m", bind_secret_id: true, secret_id_ttl: "60s", secret_id_num_uses: 1}' \
    | api -d POST "auth/approle/role/$role" "$admin" >/dev/null
  log "approle $role: policies $pols (secret/envs/platform/$role only), token 2m, secret_id 60s / 1 use" >&2
}

# keys.sh llm-run claude|openai -- CMD...: CMD runs (port-forward kept open) with BRAVO_BAO_ADDR, BRAVO_BAO_CACERT,
# BRAVO_BAO_KV_MOUNT, BRAVO_LLM_BAO_ROLE_ID and a one-use BRAVO_LLM_BAO_SECRET_ID in its env only. The credential
# itself is read by CMD (bravo_agents.llm_secret) and never passes through this script, argv or a file.
cmd_llm_run() {
  local p=${1-} admin role_id secret_id
  [[ $p == claude || $p == openai ]] || die "usage: keys.sh llm-run claude|openai -- <cmd> [args...]"
  shift; [[ ${1-} == -- ]] && shift
  (($#)) || die "usage: keys.sh llm-run claude|openai -- <cmd> [args...]"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  api GET "secret/metadata/envs/platform/llm-$p" "$admin" >/dev/null 2>&1 \
    || die "secret/envs/platform/llm-$p is not stored (토큰 미저장)"
  llm_role_setup "$p" "$admin"
  role_id="$(api GET "auth/approle/role/llm-$p/role-id" "$admin" | jq -er .data.role_id)"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST "auth/approle/role/llm-$p/secret-id" "$admin" | jq -er .data.secret_id)"
  admin=""
  BRAVO_BAO_ADDR="$BAO_ADDR" BRAVO_BAO_CACERT="$TLS_DIR/ca.crt" BRAVO_BAO_KV_MOUNT=secret \
    BRAVO_LLM_BAO_ROLE_ID="$role_id" BRAVO_LLM_BAO_SECRET_ID="$secret_id" "$@"
}

cmd_llm_status() {
  need_custody; need_unsealed
  local admin p md; admin="$(custody_get .admin_token)"
  for p in claude openai; do
    if md="$(api GET "secret/metadata/envs/platform/llm-$p" "$admin" 2>/dev/null)"; then
      printf 'secret/envs/platform/llm-%s: stored, version %s, updated %s\n' "$p" "$(jfield "$md" .data.current_version)" \
        "$(jfield "$md" .data.updated_time)"
    else
      printf 'secret/envs/platform/llm-%s: not stored\n' "$p"
    fi
  done
}

# keys.sh env-replace <env> <name> <field>: like env-put, but overwrites a different existing value. Only for
# credentials that rotate by design (e.g. a TokenRequest token). env-put keeps refusing silent overwrites.
cmd_env_replace() {
  local env=${1-} name=${2-} field=${3-} admin
  env_path_check "$env" "$name" "$field"
  [[ ! -t 0 ]] || die "env-put reads the value from a pipe, not a terminal"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  if api GET "secret/metadata/envs/$env/$name" "$admin" >/dev/null 2>&1; then
    jq -Rs --arg f "$field" '{data: {($f): .}}' | api -p PATCH "secret/data/envs/$env/$name" "$admin" >/dev/null
  else
    jq -Rs --arg f "$field" '{data: {($f): .}}' | api -d POST "secret/data/envs/$env/$name" "$admin" >/dev/null
  fi
  log "replaced secret/envs/$env/$name field $field from stdin (value not shown)"
}

# keys.sh env-put-json <env> <name>: a JSON object on stdin (a-z0-9_ field names, non-empty string values; null
# removes that field) merged into secret/envs/<env>/<name>. For credentials that come as several fields, e.g. the
# ChatGPT session from llm-login. Values move on pipes only (no here-strings: they can land in a temp file).
cmd_env_put_json() {
  local env=${1-} name=${2-} admin body fields ver
  env_path_check "$env" "$name" x
  [[ ! -t 0 ]] || die "env-put-json reads a JSON object from a pipe, not a terminal"
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  body="$(jq -ce 'if type == "object" and length > 0 and all(to_entries[]; (.key | test("^[a-z0-9_]{1,64}$"))
      and (.value == null or ((.value | type) == "string" and (.value | length) > 0))) then {data: .} else error("x") end' \
      2>/dev/null)" || die "env-put-json: stdin must be one JSON object of a-z0-9_ fields with non-empty string or null values"
  fields="$(printf '%s' "$body" | jq -r '.data | to_entries | map(.key + (if .value == null then "(removed)" else "" end)) | join(",")')"
  if api GET "secret/metadata/envs/$env/$name" "$admin" >/dev/null 2>&1; then
    ver="$(printf '%s' "$body" | api -p PATCH "secret/data/envs/$env/$name" "$admin" | jq -r .data.version)"
  else
    ver="$(printf '%s' "$body" | jq -c '{data: (.data | with_entries(select(.value != null)))}' \
      | api -d POST "secret/data/envs/$env/$name" "$admin" | jq -r .data.version)"
  fi
  body=""
  log "wrote secret/envs/$env/$name fields $fields from stdin, version $ver (values not shown)"
}

# keys.sh llm-login openai [--url-file FILE] [--timeout S]: demo only (13 6, ADR 0009). Signs a separate platform
# ChatGPT session in with the Codex CLI's own OAuth flow (`codex login` with CODEX_HOME in a fresh temp dir, so
# ~/.codex is never touched) and stores it in secret/envs/platform/llm-openai. The temp dir is deleted at the end.
# A stored api_key is kept and still wins (production alternative). A password, email code or MFA prompt in the
# browser is for the account owner.
cmd_llm_login() {
  [[ ${1-} == openai ]] || die "usage: keys.sh llm-login openai [--url-file FILE] [--timeout SECONDS]"
  shift
  command -v codex >/dev/null || die "llm-login needs the codex CLI"
  need_custody; need_unsealed
  python3 -I "$SCRIPT_DIR/codex_login.py" "$@" | cmd_env_put_json platform llm-openai
}

# keys.sh env-fields <env> <name>: field names of secret/envs/<env>/<name> on stdout, one per line (no values).
cmd_env_fields() {
  local env=${1-} name=${2-} admin
  env_path_check "$env" "$name" x
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  api GET "secret/data/envs/$env/$name" "$admin" 2>/dev/null | jq -r '.data.data | keys[]' || true
}

# Names only (KV v2 metadata LIST): what an environment's secret_ref may point at. Never reads a value.
cmd_env_list() {
  local env=${1-} admin path=envs
  if [[ -n $env ]]; then
    [[ $env =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "usage: keys.sh env-list [env]"
    path="envs/$env"
  fi
  need_custody; need_unsealed
  admin="$(custody_get .admin_token)"
  api GET "secret/metadata/$path?list=true" "$admin" 2>/dev/null | jq -r --arg p "$path" '.data.keys[] | "\($p)/\(.)"' || true
}

# keys.sh k8s-auth-setup: Kubernetes auth on the platform OpenBao for in-cluster services (platform-rke2 only).
#   auth/kubernetes: kubernetes_host https://kubernetes.default.svc, CA = the platform cluster CA (public),
#   disable_local_ca_jwt: the client's own service-account JWT is the TokenReview credential (the service account
#   is bound to system:auth-delegator; OpenBao gets no Kubernetes token of its own).
#   role rehearsal: SA bravo-control/rehearsal -> policy platform-rehearsal (deploy/pilot/bao/policies), token 1h/4h.
# Enabling an auth method needs root: break-glass root from the custody shares, revoked at the end.
cmd_k8s_auth_setup() {
  [[ $BAO_TARGET == platform-rke2 ]] || die "k8s-auth-setup is for --target platform-rke2"
  need_custody; need_unsealed
  local admin root ca pol
  admin="$(custody_get .admin_token)"
  ca="$(kubectl config view --raw -o json | jq -r --arg c "$PLATFORM_CONTEXT" \
    '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]' | base64 -d)"
  [[ $ca == *"BEGIN CERTIFICATE"* ]] || die "no CA for context $PLATFORM_CONTEXT"
  pol="$PILOT_DIR/bao/policies/platform-rehearsal.hcl"
  [[ -f $pol ]] && put_policy platform-rehearsal "$(cat "$pol")" "$admin"
  if ! api GET sys/auth "$admin" | jq -e '(.data // .) | has("kubernetes/")' >/dev/null; then
    cmd_generate_root >/dev/null
    root="$(custody_get .root_token)"
    printf '{"type":"kubernetes","description":"platform in-cluster services (D-26)"}' | api -d POST sys/auth/kubernetes "$root" >/dev/null
    api POST auth/token/revoke-self "$root" >/dev/null
    jq -n --arg at "$(now)" '{root_token_revoked_at: $at}' | custody_merge
    log "auth: kubernetes enabled (root used once, revoked)"
  fi
  jq -n --arg ca "$ca" '{kubernetes_host: "https://kubernetes.default.svc", kubernetes_ca_cert: $ca, disable_local_ca_jwt: true}' \
    | api -d POST auth/kubernetes/config "$admin" >/dev/null 2>&1 || {
      cmd_generate_root >/dev/null; root="$(custody_get .root_token)"
      jq -n --arg ca "$ca" '{kubernetes_host: "https://kubernetes.default.svc", kubernetes_ca_cert: $ca, disable_local_ca_jwt: true}' \
        | api -d POST auth/kubernetes/config "$root" >/dev/null
      printf '%s' '{"bound_service_account_names":["rehearsal"],"bound_service_account_namespaces":["bravo-control"],
        "token_policies":["platform-rehearsal"],"token_ttl":"1h","token_max_ttl":"4h","token_type":"service"}' \
        | api -d POST auth/kubernetes/role/rehearsal "$root" >/dev/null
      api POST auth/token/revoke-self "$root" >/dev/null
      jq -n --arg at "$(now)" '{root_token_revoked_at: $at}' | custody_merge
      log "auth/kubernetes config + role rehearsal written with root (revoked)"
      k8s_auth_roles_admin "$admin"
      return 0; }
  printf '%s' '{"bound_service_account_names":["rehearsal"],"bound_service_account_namespaces":["bravo-control"],
    "token_policies":["platform-rehearsal"],"token_ttl":"1h","token_max_ttl":"4h","token_type":"service"}' \
    | api -d POST auth/kubernetes/role/rehearsal "$admin" >/dev/null
  log "auth/kubernetes config + role rehearsal (SA bravo-control/rehearsal -> platform-rehearsal)"
  k8s_auth_roles_admin "$admin"
}

# Platform API: environment secret_ref check reads KV metadata only (15-r2-contracts, platform/api secretrefs.py).
POLICY_API_SECRETREF='# Platform API (D-26): existence/version of environment chat-db secrets, never their values.
path "secret/metadata/envs/+/chat-db" { capabilities = ["read"] }'

k8s_auth_roles_admin() { # roles writable with the platform-admin token (auth/kubernetes/role/* is under auth/)
  local admin=$1
  put_policy platform-api-secretref "$POLICY_API_SECRETREF" "$admin"
  printf '%s' '{"bound_service_account_names":["api"],"bound_service_account_namespaces":["bravo-control"],
    "token_policies":["platform-api-secretref"],"token_ttl":"1h","token_max_ttl":"4h","token_type":"service"}' \
    | api -d POST auth/kubernetes/role/api "$admin" >/dev/null 2>&1 && { log "auth/kubernetes role api (SA bravo-control/api -> platform-api-secretref)"; return 0; }
  local root
  cmd_generate_root >/dev/null; root="$(custody_get .root_token)"
  printf '%s' '{"bound_service_account_names":["api"],"bound_service_account_namespaces":["bravo-control"],
    "token_policies":["platform-api-secretref"],"token_ttl":"1h","token_max_ttl":"4h","token_type":"service"}' \
    | api -d POST auth/kubernetes/role/api "$root" >/dev/null
  api POST auth/token/revoke-self "$root" >/dev/null
  jq -n --arg at "$(now)" '{root_token_revoked_at: $at}' | custody_merge
  log "auth/kubernetes role api written with root (revoked)"
}

# --- platform-eks (deploy/platform/openbao) --------------------------------------------------------------

# CA + server cert for the platform OpenBao. The server key goes only into the cluster Secret
# openbao/openbao-tls (EKS envelope-encrypted with KMS). The CA key is not kept (rotation = new CA);
# ca.crt stays in custody for clients and is published as ConfigMap openbao-ca (not secret).
cmd_tls_platform() {
  command -v kubectl >/dev/null || die "needs kubectl"
  if [[ -f $PLATFORM_TLS_DIR/ca.crt && ${1-} != --rotate ]]; then
    log "platform TLS exists ($PLATFORM_TLS_DIR/ca.crt); use --rotate to replace, then restart openbao-0"; return 0
  fi
  mkdir -p "$CUSTODY_DIR" "$PLATFORM_TLS_DIR"; chmod 700 "$CUSTODY_DIR" "$PLATFORM_TLS_DIR"
  local d; d="$(mktemp -d "$CUSTODY_DIR/.tls.XXXXXX")"
  local sans="DNS:openbao,DNS:openbao.openbao,DNS:openbao.openbao.svc,DNS:openbao.openbao.svc.cluster.local"
  sans="$sans,DNS:openbao-0.openbao-internal.openbao.svc,DNS:openbao-0.openbao-internal.openbao.svc.cluster.local"
  sans="$sans,DNS:localhost,IP:127.0.0.1"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 90 \
    -subj "/CN=bravo $BAO_TARGET OpenBao CA" -keyout "$d/ca.key" -out "$d/ca.crt" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -subj "/CN=openbao.openbao.svc" -keyout "$d/tls.key" -out "$d/tls.csr" 2>/dev/null
  openssl x509 -req -in "$d/tls.csr" -CA "$d/ca.crt" -CAkey "$d/ca.key" -days 30 \
    -set_serial "0x$(openssl rand -hex 16)" -out "$d/tls.crt" -extfile <(printf '%s\n' \
      "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
      "extendedKeyUsage=serverAuth" "subjectAltName=$sans") 2>/dev/null
  kubectl --context "$PLATFORM_CONTEXT" -n openbao create secret generic openbao-tls \
    --from-file=tls.crt="$d/tls.crt" --from-file=tls.key="$d/tls.key" --from-file=ca.crt="$d/ca.crt" \
    --dry-run=client -o yaml | kubectl --context "$PLATFORM_CONTEXT" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null
  cp "$d/ca.crt" "$PLATFORM_TLS_DIR/ca.crt"; chmod 600 "$PLATFORM_TLS_DIR/ca.crt"
  rm -rf "$d"
  local ns
  for ns in openbao bravo-control; do
    kubectl --context "$PLATFORM_CONTEXT" get ns "$ns" >/dev/null 2>&1 || continue
    kubectl --context "$PLATFORM_CONTEXT" -n "$ns" create configmap openbao-ca --from-file=ca.crt="$PLATFORM_TLS_DIR/ca.crt" \
      --dry-run=client -o yaml | kubectl --context "$PLATFORM_CONTEXT" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null
  done
  log "platform TLS: Secret openbao/openbao-tls (server cert 30 days), CA in $PLATFORM_TLS_DIR/ca.crt and ConfigMap openbao-ca"
}

# Move the pilot store to the platform EKS by raft snapshot, so the Shamir shares in custody keep working.
#  1 pilot: break-glass root (custody only) -> raft snapshot to a 600 file in custody (barrier-encrypted)
#  2 pilot: revoke that root there, stop the container (sealed from then on: no write after the snapshot)
#  3 platform: throwaway init (1 share, file in custody) -> unseal -> snapshot-force restore
#  4 platform: unseal with the custody shares, check the custody admin token and the secret tree
#  5 revoke the root on the platform, delete the snapshot and throwaway init, custody bao_target=platform-eks
cmd_migrate_to_platform() {
  # keys.sh migrate-to-platform [--from pilot|platform-eks] [--to platform-eks|platform-rke2]
  local from=pilot to=platform-eks
  while (($#)); do case $1 in --from) from=$2; shift 2 ;; --to) to=$2; shift 2 ;; *) die "usage: migrate-to-platform [--from T] [--to T]" ;; esac; done
  [[ $from != "$to" && $to != pilot ]] || die "bad --from/--to"
  need_custody
  [[ $(jq -r '.bao_target // "pilot"' "$CUSTODY") != "$to" ]] || die "custody already says bao_target=$to"
  local ts snap boot admin root code st before after cid tmproot tmpkey
  ts="$(date -u +%Y%m%dT%H%M%SZ)"; snap="$CUSTODY_DIR/.migrate-$ts.snap"; boot="$CUSTODY_DIR/.$to-bootstrap-$ts.json"

  if [[ $from == pilot ]]; then use_pilot; else target_cfg "$from"; BAO_TARGET=$from; pf_start; fi
  need_unsealed
  admin="$(custody_get .admin_token)"
  before="$(api LIST secret/metadata/envs "$admin" | jq -c '.data.keys | sort')"
  cmd_generate_root
  root="$(custody_get .root_token)"
  code="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 60 -H @<(tok_hdr "$root") -o "$snap" -w '%{http_code}' \
    "$BAO_ADDR/v1/sys/storage/raft/snapshot")" || die "snapshot request failed"
  [[ $code == 200 && -s $snap ]] || { rm -f "$snap"; die "snapshot HTTP $code"; }
  chmod 600 "$snap"
  log "$from: raft snapshot taken ($(wc -c <"$snap" | tr -d ' ') bytes, barrier-encrypted, $snap)"
  api POST auth/token/revoke-self "$root" >/dev/null || die "could not revoke the $from root token"
  if [[ $from == pilot ]]; then
    cid="$(docker ps -q --filter label=com.docker.compose.project=bravo-pilot --filter label=com.docker.compose.service=platform-bao)"
    [[ -n $cid ]] && docker stop "$cid" >/dev/null && log "pilot: root revoked, container platform-bao stopped (sealed; volume kept for rollback)"
  else
    # No write after the snapshot: stop the source pod (StatefulSet to 0; the PVC stays for rollback).
    pf_stop
    kubectl --context "$PLATFORM_CONTEXT" -n openbao scale statefulset openbao --replicas=0 >/dev/null
    kubectl --context "$PLATFORM_CONTEXT" -n openbao wait --for=delete pod/openbao-0 --timeout=120s >/dev/null 2>&1 || true
    log "$from: root revoked, openbao scaled to 0 (PVC kept for rollback)"
  fi

  target_cfg "$to"; BAO_TARGET=$to
  pf_start
  st="$(seal_state)"
  [[ $(jfield "$st" .initialized) == false ]] || die "platform OpenBao is already initialised; refusing to restore over it"
  printf '{"secret_shares":1,"secret_threshold":1}' | api -d PUT sys/init >"$boot"; chmod 600 "$boot"
  tmpkey="$(jq -er '.keys_base64[0]' "$boot")"; tmproot="$(jq -er .root_token "$boot")"
  printf '%s' "$tmpkey" | jq -Rs '{key: .}' | api -d PUT sys/unseal >/dev/null
  for _ in $(seq 1 40); do [[ $(curl -s -o /dev/null -w '%{http_code}' --cacert "$TLS_DIR/ca.crt" "$BAO_ADDR/v1/sys/health") == 200 ]] && break; sleep 0.5; done
  code="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 120 -X POST -H @<(tok_hdr "$tmproot") \
    -H 'Content-Type: application/octet-stream' --data-binary @"$snap" -o /dev/null -w '%{http_code}' \
    "$BAO_ADDR/v1/sys/storage/raft/snapshot-force")" || die "restore request failed"
  [[ $code == 2* ]] || die "snapshot-force HTTP $code (snapshot kept at $snap, throwaway init at $boot)"
  tmpkey=""; tmproot=""
  log "platform: snapshot restored"
  # The node keeps the throwaway seal config in memory until it restarts (seen 2026-11-02: unseal with a
  # custody share failed "invalid key size 33" until restart). Restart the pod, then unseal with custody.
  pf_stop
  kubectl --context "$PLATFORM_CONTEXT" -n openbao delete pod openbao-0 --wait=true >/dev/null
  kubectl --context "$PLATFORM_CONTEXT" -n openbao wait --for=condition=PodScheduled pod/openbao-0 --timeout=120s >/dev/null
  for _ in $(seq 1 60); do
    [[ $(kubectl --context "$PLATFORM_CONTEXT" -n openbao get pod openbao-0 -o jsonpath='{.status.containerStatuses[0].started}' 2>/dev/null) == true ]] && break
    sleep 1
  done
  pf_start
  [[ $(jfield "$(seal_state)" .n) == "$(custody_get '.keys_base64 | length')" ]] || die "restored seal config does not match custody"
  cmd_unseal
  api GET auth/token/lookup-self "$admin" >/dev/null || die "custody admin token does not work on the platform store"
  after="$(api LIST secret/metadata/envs "$admin" | jq -c '.data.keys | sort')"
  [[ $before == "$after" ]] || die "secret/envs listing differs after restore"
  log "platform: custody shares unseal it, admin token works, secret/envs listing matches ($(jq length <<<"$after") entries)"
  api POST auth/token/revoke-self "$root" >/dev/null || die "could not revoke the root token on the platform store"
  rm -f "$snap" "$boot"
  jq -n --arg at "$(now)" --arg ctx "$PLATFORM_CONTEXT" --arg to "$to" --arg from "$from" \
    '{bao_target: $to, bao_addr: ("kubectl --context " + $ctx + " -n openbao port-forward pod/openbao-0"),
      migrated_at: $at, migrated_from: ($from + " (raft snapshot)"), root_token_revoked_at: $at}' | custody_merge
  log "done: custody bao_target=$to; root revoked on both stores; snapshot and throwaway init deleted"
}

for bin in curl jq openssl; do command -v "$bin" >/dev/null || die "needs $bin"; done
cmd=${1-help}; shift || true
case $cmd in
  tls | migrate-to-platform | help | -h | --help) ;;
  *) [[ $BAO_TARGET == pilot ]] || pf_start ;;
esac
[[ $cmd == tls && $BAO_TARGET == platform-* ]] && cmd=tls-platform
case $cmd in
  tls) cmd_tls "$@" ;;
  init) cmd_init ;;
  unseal) cmd_unseal ;;
  status) cmd_status ;;
  import-cloudflare) cmd_import_cloudflare ;;
  with-cloudflare) cmd_with_cloudflare "$@" ;;
  issue-tunnel-token) cmd_issue_tunnel_token "$@" ;;
  env-delete) cmd_env_delete "$@" ;;
  issue-service-token) cmd_issue_service_token "$@" ;;
  generate-root) cmd_generate_root ;;
  revoke-root) cmd_revoke_root ;;
  env-generate) cmd_env_generate "$@" ;;
  env-pipe) cmd_env_pipe "$@" ;;
  env-copy) cmd_env_copy "$@" ;;
  env-put) cmd_env_put "$@" ;;
  env-replace) cmd_env_replace "$@" ;;
  env-fields) cmd_env_fields "$@" ;;
  k8s-auth-setup) cmd_k8s_auth_setup ;;
  env-list) cmd_env_list "$@" ;;
  llm-run) cmd_llm_run "$@" ;;
  llm-status) cmd_llm_status ;;
  llm-login) cmd_llm_login "$@" ;;
  env-put-json) cmd_env_put_json "$@" ;;
  tls-platform) cmd_tls_platform "$@" ;;
  migrate-to-platform) cmd_migrate_to_platform "$@" ;;
  signer-init) cmd_signer_init ;;
  with-signer) cmd_with_signer "$@" ;;
  signer-pubkey) cmd_signer_pubkey "$@" ;;
  help | -h | --help) sed -n '2,41p' "${BASH_SOURCE[0]}" ;;
  *) die "unknown command: $cmd (keys.sh help)" ;;
esac
