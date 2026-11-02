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
#   keys.sh issue-tunnel-token ENV TUNNEL_ID   Cloudflare tunnel token -> secret/envs/ENV/cf-tunnel
#   keys.sh generate-root | revoke-root        break-glass root token (custody only) and its revocation
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
BAO_ADDR="${BAO_ADDR:-https://127.0.0.1:${BAO_PORT:-8200}}"

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
  CLOUDFLARE_API_TOKEN="$CF_TOKEN" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT" CLOUDFLARE_ZONE_ID="$CF_ZONE" exec "$@"
}

cmd_issue_tunnel_token() {
  local env=${1-} tid=${2-}
  [[ $env =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "usage: keys.sh issue-tunnel-token <env> <tunnel_id> (env: a-z 0-9 -)"
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
    | api -d POST "secret/data/envs/$env/cf-tunnel" "$EDGE_TOKEN" \
    | jq -r '"written secret/envs/'"$env"'/cf-tunnel version \(.data.version) (field token, value not shown)"'
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

for bin in curl jq openssl; do command -v "$bin" >/dev/null || die "needs $bin"; done
cmd=${1-help}; shift || true
case $cmd in
  tls) cmd_tls "$@" ;;
  init) cmd_init ;;
  unseal) cmd_unseal ;;
  status) cmd_status ;;
  import-cloudflare) cmd_import_cloudflare ;;
  with-cloudflare) cmd_with_cloudflare "$@" ;;
  issue-tunnel-token) cmd_issue_tunnel_token "$@" ;;
  generate-root) cmd_generate_root ;;
  revoke-root) cmd_revoke_root ;;
  help | -h | --help) sed -n '2,13p' "${BASH_SOURCE[0]}" ;;
  *) die "unknown command: $cmd (keys.sh help)" ;;
esac
