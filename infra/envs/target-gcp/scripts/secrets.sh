#!/usr/bin/env bash
# Platform-side secrets for target-gcp (D-26). Values go only into the platform OpenBao, through pipes.
# Nothing is printed, written to a file, passed as an argument, or kept in Terraform state.
#
#   secrets.sh seed       create missing values under secret/envs/target-gcp/*:
#                           chat-db (app_writer, replicator, executor_ctl, executor_observe, pgb_admin,
#                           pgb_stats, db_owner; same field layout as source-aws and target-onprem),
#                           chat-admin (token),
#                           object-store (CLOUD-E2E-CONTRACT §9: bucket, endpoint_url, region, access_key_id,
#                           secret_access_key = GCS HMAC of SA chat-app), gate-other-s3 (HMAC of SA gate-other,
#                           OBJ-05 only).
#                         cf-tunnel is written by deploy/pilot/scripts/keys.sh issue-tunnel-token.
#   secrets.sh eso        policy + AppRole eso-target-gcp (read secret/envs/target-gcp/* only), then a fresh
#                         secret_id into Secret external-secrets/platform-bao-approle via kubectl stdin.
#   secrets.sh status     which fields exist (set/empty), HMAC key state. No values.
#   secrets.sh revoke     teardown: delete HMAC keys in GCS, the object-store/gate-other-s3 entries, the AppRole.
source "$(dirname "$0")/lib.sh"

rand() { openssl rand -hex 24; }   # 48 hex chars: no quotes, safe for userlist.txt and psql variables

# Missing fields only (KV v2 merge-patch); existing values are never rotated by a re-run.
seed_fields() {   # seed_fields NAME TOKEN field...
  local name=$1 token=$2 have f add=(); shift 2
  have="$(bao GET "secret/data/envs/$BAO_ENV/$name" "$token" 2>/dev/null | jq -c '.data.data // {}' || echo '{}')"
  for f in "$@"; do
    [[ $(printf '%s' "$have" | jq -r --arg f "$f" '.[$f] // ""') == "" ]] && add+=("$f")
  done
  ((${#add[@]})) || { log "secret/envs/$BAO_ENV/$name complete (kept)"; return 0; }
  local body; body="$(for f in "${add[@]}"; do printf '%s\t%s\n' "$f" "$(rand)"; done \
    | jq -Rn '{data: ([inputs | split("\t") | {(.[0]): .[1]}] | add)}')"
  if [[ $have == '{}' ]]; then
    printf '%s' "$body" | bao -d POST "secret/data/envs/$BAO_ENV/$name" "$token" >/dev/null
  else
    printf '%s' "$body" | bao -p PATCH "secret/data/envs/$BAO_ENV/$name" "$token" >/dev/null
  fi
  unset body
  log "secret/envs/$BAO_ENV/$name: generated ${add[*]} (values not shown)"
}

# The destination app reaches the source DB through PgBouncer before cutover (target-src) and its own DB after
# (target-dst) with ONE userlist entry, so app_writer must be the source's password. Copied OpenBao -> OpenBao.
copy_app_writer() {   # copy_app_writer FROM_ENV TOKEN
  local from=$1 token=$2 v
  v="$(bao GET "secret/data/envs/$from/chat-db" "$token" 2>/dev/null | jq -r '.data.data.app_writer // ""')" || v=""
  if [[ -z $v ]]; then log "secret/envs/$from/chat-db has no app_writer: target-gcp keeps its own (연결 안 됨 for target-src)"; return 0; fi
  if [[ $(bao GET "secret/data/envs/$BAO_ENV/chat-db" "$token" | jq -r '.data.data.app_writer // ""') == "$v" ]]; then
    v=""; log "app_writer already equals secret/envs/$from/chat-db (kept)"; return 0
  fi
  printf '%s' "$v" | jq -Rs '{data: {app_writer: .}}' | bao -p PATCH "secret/data/envs/$BAO_ENV/chat-db" "$token" >/dev/null
  v=""; log "secret/envs/$BAO_ENV/chat-db app_writer <- secret/envs/$from/chat-db (value not shown)"
}

seed_random() {   # seed_random NAME TOKEN field...
  local name=$1 token=$2; shift 2
  if bao_exists "$name" "$token"; then log "secret/envs/$BAO_ENV/$name exists (kept)"; return 0; fi
  local f; for f in "$@"; do printf '%s\t%s\n' "$f" "$(rand)"; done \
    | jq -Rn '{data: ([inputs | split("\t") | {(.[0]): .[1]}] | add)}' \
    | bao -d POST "secret/data/envs/$BAO_ENV/$name" "$token" >/dev/null
  log "secret/envs/$BAO_ENV/$name written (fields: $*; values not shown)"
}

hmac_state() {  # hmac_state ACCESS_ID -> ACTIVE | INACTIVE | missing
  gcloud storage hmac describe "$1" --project "$PROJECT" --format='value(state)' 2>/dev/null || echo missing
}

seed_hmac() {   # seed_hmac NAME SA_EMAIL TOKEN [extra JSON fields]
  local name=$1 sa=$2 token=$3 extra=${4:-'{}'} cur
  if cur="$(bao GET "secret/data/envs/$BAO_ENV/$name" "$token" 2>/dev/null)"; then
    # pipe, not a here-string: bash 3.2 writes here-strings to a temp file
    local id; id="$(printf '%s' "$cur" | jq -r '.data.data.access_key_id // ""')"
    if [[ -n $id && $(hmac_state "$id") == ACTIVE ]]; then log "secret/envs/$BAO_ENV/$name: HMAC key ACTIVE (kept)"; return 0; fi
    log "secret/envs/$BAO_ENV/$name: stored HMAC key not ACTIVE in GCS, issuing a new one"
  fi
  # The create response holds the secret: it goes straight from gcloud through jq into OpenBao.
  gcloud storage hmac create "$sa" --project "$PROJECT" --format=json \
    | jq --argjson extra "$extra" '{data: ($extra + {access_key_id: .metadata.accessId, secret_access_key: .secret})}' \
    | bao -d POST "secret/data/envs/$BAO_ENV/$name" "$token" >/dev/null
  log "secret/envs/$BAO_ENV/$name written (GCS HMAC of $sa; values not shown)"
}

prune_hmac() {  # prune_hmac SA KEEP_ID: delete every other HMAC key of SA (no orphan credentials)
  local sa=$1 keep=$2 id
  # without --all the list holds ACTIVE and INACTIVE keys only (DELETED ones are gone already)
  for id in $(gcloud storage hmac list --project "$PROJECT" --service-account "$sa" --format='value(accessId)' 2>/dev/null); do
    [[ $id == "$keep" ]] && continue
    gcloud storage hmac update "$id" --deactivate --project "$PROJECT" >/dev/null 2>&1 || true
    if gcloud storage hmac delete "$id" --project "$PROJECT" >/dev/null 2>&1; then log "removed extra HMAC key of $sa"; fi
  done
  return 0
}

cmd_seed() {
  need_bao; local t; t="$(bao_admin_token)"
  seed_fields chat-db "$t" app_writer replicator executor_ctl executor_observe pgb_admin pgb_stats db_owner
  [[ -z ${BRAVO_APP_WRITER_FROM-source-aws} ]] || copy_app_writer "${BRAVO_APP_WRITER_FROM-source-aws}" "$t"
  if bao_exists chat-pgbouncer "$t"; then   # earlier layout: pgb_* now live in chat-db like the other envs
    bao DELETE "secret/metadata/envs/$BAO_ENV/chat-pgbouncer" "$t" >/dev/null && log "old secret/envs/$BAO_ENV/chat-pgbouncer removed"
  fi
  seed_random chat-admin "$t" token
  local store
  store="$(jq -nc --arg b "$ATTACH_BUCKET" '{bucket: $b, endpoint_url: "https://storage.googleapis.com", region: "auto"}')"
  seed_hmac object-store "$CHAT_APP_SA" "$t" "$store"
  seed_hmac gate-other-s3 "$GATE_OTHER_SA" "$t"
  # earlier layout (before the contract named object-store): remove, its key is revoked
  if bao_exists chat-s3 "$t"; then
    local old; old="$(bao GET "secret/data/envs/$BAO_ENV/chat-s3" "$t" | jq -r '.data.data["access-key-id"] // ""')"
    [[ -z $old ]] || { gcloud storage hmac update "$old" --deactivate --project "$PROJECT" >/dev/null 2>&1 || true
                       gcloud storage hmac delete "$old" --project "$PROJECT" >/dev/null 2>&1 || true; }
    bao DELETE "secret/metadata/envs/$BAO_ENV/chat-s3" "$t" >/dev/null && log "old secret/envs/$BAO_ENV/chat-s3 and its HMAC key removed"
  fi
  prune_hmac "$CHAT_APP_SA" "$(bao GET "secret/data/envs/$BAO_ENV/object-store" "$t" | jq -r .data.data.access_key_id)"
  prune_hmac "$GATE_OTHER_SA" "$(bao GET "secret/data/envs/$BAO_ENV/gate-other-s3" "$t" | jq -r .data.data.access_key_id)"
  # gate-other-s3 from the earlier layout used dashed field names: reissue with the contract names
  if bao_exists gate-other-s3 "$t" && [[ -z $(bao GET "secret/data/envs/$BAO_ENV/gate-other-s3" "$t" | jq -r '.data.data.access_key_id // ""') ]]; then
    local oid; oid="$(bao GET "secret/data/envs/$BAO_ENV/gate-other-s3" "$t" | jq -r '.data.data["access-key-id"] // ""')"
    [[ -z $oid ]] || { gcloud storage hmac update "$oid" --deactivate --project "$PROJECT" >/dev/null 2>&1 || true
                       gcloud storage hmac delete "$oid" --project "$PROJECT" >/dev/null 2>&1 || true; }
    bao DELETE "secret/metadata/envs/$BAO_ENV/gate-other-s3" "$t" >/dev/null
    seed_hmac gate-other-s3 "$GATE_OTHER_SA" "$t"
  fi
  bao_exists cf-tunnel "$t" || log "WARNING secret/envs/$BAO_ENV/cf-tunnel missing: keys.sh issue-tunnel-token $BAO_ENV <tunnel_id>"
}

POLICY_ESO='# ESO on GKE target-gcp (D-26): read this environment path only.
path "secret/data/envs/target-gcp/*" { capabilities = ["read"] }
path "secret/metadata/envs/target-gcp/*" { capabilities = ["read", "list"] }'

cmd_eso() {
  need_bao; local t; t="$(bao_admin_token)"
  jq -n --arg p "$POLICY_ESO" '{policy: $p}' | bao -d PUT sys/policies/acl/eso-target-gcp "$t" >/dev/null
  printf '%s' '{"token_policies":["eso-target-gcp"],"token_type":"service","token_ttl":"20m","token_max_ttl":"1h",
    "bind_secret_id":true,"secret_id_ttl":"168h","secret_id_num_uses":0}' \
    | bao -d POST auth/approle/role/eso-target-gcp "$t" >/dev/null
  log "OpenBao: policy + AppRole eso-target-gcp (token 20m, secret_id 168h)"
  k get ns external-secrets >/dev/null || die "namespace external-secrets missing (install ESO first)"
  local role_id
  role_id="$(bao GET auth/approle/role/eso-target-gcp/role-id "$t" | jq -er .data.role_id)"
  # secret_id: OpenBao -> jq -> kubectl apply (stdin). Not printed, not in argv.
  printf '{}' | bao -d POST auth/approle/role/eso-target-gcp/secret-id "$t" \
    | jq --arg rid "$role_id" '{apiVersion: "v1", kind: "Secret", type: "Opaque",
        metadata: {name: "platform-bao-approle", namespace: "external-secrets",
                   labels: {"app.kubernetes.io/part-of": "bravo-landing-zone"}},
        stringData: {"role-id": $rid, "secret-id": .data.secret_id}}' \
    | k apply -f - >/dev/null
  log "Secret external-secrets/platform-bao-approle applied (fresh secret_id, values not shown)"
}

cmd_status() {
  need_bao; local t n; t="$(bao_admin_token)"
  for n in chat-db chat-admin object-store gate-other-s3 cf-tunnel; do
    if bao_exists "$n" "$t"; then
      printf 'secret/envs/%s/%s: %s\n' "$BAO_ENV" "$n" \
        "$(bao GET "secret/data/envs/$BAO_ENV/$n" "$t" | jq -c '.data.data | to_entries | map("\(.key)=\(if .value == "" then "empty" else "set" end)")')"
    else
      printf 'secret/envs/%s/%s: missing\n' "$BAO_ENV" "$n"
    fi
  done
  for n in object-store gate-other-s3; do
    bao_exists "$n" "$t" || continue
    printf '%s HMAC state: %s\n' "$n" "$(hmac_state "$(bao GET "secret/data/envs/$BAO_ENV/$n" "$t" | jq -r '.data.data.access_key_id')")"
  done
}

cmd_revoke() {
  need_bao; local t sa id; t="$(bao_admin_token)"
  for sa in "$CHAT_APP_SA" "$GATE_OTHER_SA"; do
    for id in $(gcloud storage hmac list --project "$PROJECT" --service-account "$sa" --format='value(accessId)' 2>/dev/null); do
      gcloud storage hmac update "$id" --deactivate --project "$PROJECT" >/dev/null 2>&1 || true
      gcloud storage hmac delete "$id" --project "$PROJECT" >/dev/null 2>&1 && log "HMAC key of $sa deleted"
    done
  done
  for n in object-store gate-other-s3; do
    bao DELETE "secret/metadata/envs/$BAO_ENV/$n" "$t" >/dev/null 2>&1 && log "secret/envs/$BAO_ENV/$n deleted"
  done
  bao DELETE auth/approle/role/eso-target-gcp "$t" >/dev/null 2>&1 && log "AppRole eso-target-gcp deleted"
  return 0
}

load_outputs
case ${1-} in
  seed) cmd_seed ;;
  eso) cmd_eso ;;
  status) cmd_status ;;
  revoke) cmd_revoke ;;
  *) die "usage: secrets.sh seed|eso|status|revoke" ;;
esac
