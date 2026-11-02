#!/usr/bin/env bash
# T9 (spec r2): copy the source app's AWS Secrets Manager values once into the platform OpenBao
# (D-26) at secret/envs/<target-env>/chat/<name>. Values travel only through pipes:
#   aws secretsmanager get-secret-value (stdout) | jq | curl --data-binary @- (stdin)
# They never go to argv, files, shell variables or logs. Only names, ARNs, version ids, KV versions
# and "match/mismatch" are printed.
#
#   migrate-secrets.sh plan  [--prefix P] [--env E]   list what would be copied (metadata only)
#   migrate-secrets.sh apply [--prefix P] [--env E]   copy, then compare sha256 of source and target
#   migrate-secrets.sh setup                          write policy + AppRole (admin token from custody)
#
# Defaults: --prefix bravo-source/chat/  --env target-onprem  (AWS region: $AWS_REGION or ap-northeast-2)
# Writes run as a 5-minute token of AppRole "secret-migrate" (policy platform-secret-migrate):
# create/update/read on secret/data/envs/target-onprem/chat/* only. "One time": an existing target
# with the same content is skipped; a different existing target is refused (no silent overwrite).
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PILOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TLS_DIR="$PILOT_DIR/bao/tls"
POLICY_FILE="$PILOT_DIR/bao/policies/platform-secret-migrate.hcl"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
CUSTODY="$CUSTODY_DIR/openbao-init.json"
BAO_ADDR="${BAO_ADDR:-https://127.0.0.1:${PLATFORM_BAO_HOST_PORT:-8200}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-2}}"
PREFIX="bravo-source/chat/"
TARGET_ENV="target-onprem"
POLICY=platform-secret-migrate
ROLE=secret-migrate

die() { printf 'migrate-secrets: %s\n' "$*" >&2; exit 1; }
log() { printf 'migrate-secrets: %s\n' "$*"; }

tok_hdr() { [[ -z ${1-} ]] || printf 'X-Vault-Token: %s\n' "$1"; }

# api [-d] METHOD PATH [TOKEN]; -d reads a JSON body from stdin. Token via a pipe (-H @fd), not argv.
api() {
  local body=0; [[ $1 == -d ]] && { body=1; shift; }
  local method=$1 path=$2 token=${3-} out code
  if ((body)); then
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -H 'Content-Type: application/json' --data-binary @- -w '\n%{http_code}' "$BAO_ADDR/v1/$path")" \
      || die "cannot reach OpenBao at $BAO_ADDR"
  else
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -w '\n%{http_code}' "$BAO_ADDR/v1/$path" </dev/null)" || die "cannot reach OpenBao at $BAO_ADDR"
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  if [[ $code != 2* ]]; then
    printf 'migrate-secrets: %s %s -> HTTP %s %s\n' "$method" "$path" "$code" \
      "$(printf '%s' "$out" | jq -c '.errors? // empty' 2>/dev/null || true)" >&2
    return 1
  fi
  printf '%s' "$out"
}

file_mode() { stat -f %Lp "$1" 2>/dev/null || stat -c %a "$1"; }
need_custody() {
  [[ -f $CUSTODY ]] || die "no custody file at $CUSTODY"
  [[ $(file_mode "$CUSTODY") == 600 ]] || die "custody file must be mode 600"
}
admin_token() { jq -er .admin_token "$CUSTODY"; }

need_unsealed() {
  local st; st="$(api GET sys/seal-status)"
  [[ $(jq -r .sealed <<<"$st") == false ]] || die "OpenBao is sealed: scripts/keys.sh unseal"
}

parse_args() {
  while (($#)); do
    case $1 in
      --prefix) PREFIX=${2-}; shift 2 ;;
      --env) TARGET_ENV=${2-}; shift 2 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  [[ $PREFIX =~ ^[A-Za-z0-9/_+=.@-]+/$ ]] || die "--prefix must end with / (got: $PREFIX)"
  [[ $TARGET_ENV == target-onprem ]] || die "only --env target-onprem is allowed by policy $POLICY"
}

cmd_setup() {
  need_custody; need_unsealed
  local admin; admin="$(admin_token)"
  jq -Rs '{policy: .}' "$POLICY_FILE" | api -d PUT "sys/policies/acl/$POLICY" "$admin" >/dev/null
  log "policy $POLICY written from ${POLICY_FILE#"$PILOT_DIR"/}"
  printf '%s' '{"token_policies":["'"$POLICY"'"],"token_type":"service","token_ttl":"5m","token_max_ttl":"10m",
    "bind_secret_id":true,"secret_id_ttl":"60s","secret_id_num_uses":1}' \
    | api -d POST "auth/approle/role/$ROLE" "$admin" >/dev/null
  log "approle $ROLE (policy $POLICY, token 5m, secret_id 60s / 1 use)"
}

# Sets MIG_TOKEN: admin mints one secret_id (60 s, 1 use) -> 5 min token with the narrow policy.
login() {
  need_custody; need_unsealed
  local admin role_id secret_id
  admin="$(admin_token)"
  role_id="$(api GET "auth/approle/role/$ROLE/role-id" "$admin" | jq -er .data.role_id)" \
    || die "approle $ROLE missing: migrate-secrets.sh setup"
  secret_id="$(printf '{"ttl":"60s","num_uses":1}' | api -d POST "auth/approle/role/$ROLE/secret-id" "$admin" | jq -er .data.secret_id)"
  MIG_TOKEN="$(printf '%s\n%s\n' "$role_id" "$secret_id" \
    | jq -Rn '[inputs] as $v | {role_id: $v[0], secret_id: $v[1]}' \
    | api -d POST auth/approle/login | jq -er .auth.client_token)"
}
logout() { [[ -z ${MIG_TOKEN-} ]] || api POST auth/token/revoke-self "$MIG_TOKEN" >/dev/null || true; MIG_TOKEN=""; }
trap logout EXIT

# Source secrets under PREFIX: name, ARN, current version id (metadata only, no values).
list_source() {
  aws secretsmanager list-secrets --region "$REGION" --filters "Key=name,Values=$PREFIX" --output json \
    | jq -c --arg p "$PREFIX" '.SecretList[] | select(.Name | startswith($p))
        | {name: .Name, arn: .ARN, current: ([.SecretVersionsToStages // {} | to_entries[]
           | select(.value | index("AWSCURRENT")) | .key][0] // null)}'
}

# The KV body for one secret, streamed: a JSON object SecretString becomes the KV fields,
# any other string becomes {"value": ...}, a binary secret becomes {"value_b64": ...}.
source_kv() {
  aws secretsmanager get-secret-value --region "$REGION" --secret-id "$1" --output json \
    | jq -cS '(.SecretString // null) as $s
        | if $s == null then {value_b64: .SecretBinary}
          elif ($s | (try fromjson catch null) | type) == "object" then ($s | fromjson)
          else {value: $s} end'
}
sha() { shasum -a 256 | cut -d' ' -f1; }

cmd_plan() {
  local rows; rows="$(list_source)"
  [[ -n $rows ]] || { log "no source secrets under $PREFIX in $REGION"; return 0; }
  while IFS= read -r r; do
    local name rel; name="$(jq -r .name <<<"$r")"; rel="${name#"$PREFIX"}"
    log "$(jq -r '"\(.name) (\(.arn)) current version: \(.current // "NONE - no value set")"' <<<"$r") -> secret/envs/$TARGET_ENV/chat/$rel"
  done <<<"$rows"
}

cmd_apply() {
  local rows; rows="$(list_source)"
  [[ -n $rows ]] || { log "no source secrets under $PREFIX in $REGION; nothing migrated"; return 0; }
  login
  local copied=0 skipped=0 failed=0
  while IFS= read -r r; do
    local name arn current rel path src_sha dst_sha ver
    name="$(jq -r .name <<<"$r")"; arn="$(jq -r .arn <<<"$r")"; current="$(jq -r '.current // ""' <<<"$r")"
    rel="${name#"$PREFIX"}"; path="envs/$TARGET_ENV/chat/$rel"
    [[ $rel =~ ^[A-Za-z0-9/_.-]+$ ]] || { log "SKIP $name: unsupported characters in name"; failed=$((failed + 1)); continue; }
    if [[ -z $current ]]; then
      log "NOT MIGRATED $name: the source has no AWSCURRENT value (secret container only)"; failed=$((failed + 1)); continue
    fi
    src_sha="$(source_kv "$arn" | sha)" || { log "NOT MIGRATED $name: get-secret-value failed"; failed=$((failed + 1)); continue; }
    if dst_sha="$(api GET "secret/data/$path" "$MIG_TOKEN" 2>/dev/null | jq -cS .data.data | sha)" \
       && [[ $dst_sha != "$(printf 'null\n' | sha)" ]]; then
      if [[ $dst_sha == "$src_sha" ]]; then
        log "already migrated $name -> secret/$path (content matches, source version $current)"; skipped=$((skipped + 1)); continue
      fi
      log "REFUSED $name: secret/$path exists with different content (no overwrite; one-time migration)"; failed=$((failed + 1)); continue
    fi
    # cas=0: write only if the path has no version yet (a concurrent writer cannot be overwritten).
    ver="$(source_kv "$arn" | jq -c '{options: {cas: 0}, data: .}' | api -d POST "secret/data/$path" "$MIG_TOKEN" | jq -r .data.version)" \
      || { log "NOT MIGRATED $name: OpenBao write failed"; failed=$((failed + 1)); continue; }
    dst_sha="$(api GET "secret/data/$path" "$MIG_TOKEN" | jq -cS .data.data | sha)"
    if [[ $dst_sha == "$src_sha" ]]; then
      log "migrated $name (source version $current) -> secret/$path version $ver; sha256 source == target"
      copied=$((copied + 1))
    else
      log "MISMATCH $name -> secret/$path version $ver: target content differs from source"; failed=$((failed + 1))
    fi
  done <<<"$rows"
  logout
  log "done: migrated $copied, already $skipped, not migrated $failed (values not shown)"
  ((failed == 0))
}

for bin in aws curl jq shasum; do command -v "$bin" >/dev/null || die "needs $bin"; done
cmd=${1-help}; shift || true
case $cmd in
  setup) cmd_setup ;;
  plan) parse_args "$@"; cmd_plan ;;
  apply) parse_args "$@"; cmd_apply ;;
  help | -h | --help) sed -n '2,19p' "${BASH_SOURCE[0]}" ;;
  *) die "unknown command: $cmd" ;;
esac
