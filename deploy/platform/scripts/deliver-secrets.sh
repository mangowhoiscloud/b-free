#!/usr/bin/env bash
# Platform secrets -> platform EKS Secrets (D-26, CLOUD-E2E-CONTRACT 4). The platform OpenBao is the only
# store; values move only through pipes (keys.sh env-pipe -> jq -> kubectl apply -f -). Nothing is written
# to a file, passed in argv or printed. Run from any directory; needs the bravo-platform kube context.
#
#   deliver-secrets.sh generate   create missing random values in OpenBao secret/envs/platform/* (hex)
#   deliver-secrets.sh deliver    (re)create the Kubernetes Secrets below from OpenBao
#
# OpenBao (secret/envs/...)               -> Secret (namespace/name: key)
#   platform/platform-db owner             -> bravo-control/platform-db-owner: password (+ username bravo_owner)
#   platform/platform-db api, mcp, executor, apply -> bravo-control/platform-db-roles (apply: T31 worker, 0007)
#   platform/api sse_ticket_secret         -> bravo-control/platform-api: sse_ticket_secret
#   platform/git-customer-repo private_key  -> bravo-control/git-customer-repo (read-only deploy key, collector checkout)
#   platform/rehearsal api_token, token_key -> bravo-control/platform-rehearsal (api token shared by api and rehearsal)
#   platform/grafana admin_password        -> bravo-observe/grafana-admin: admin_password
#   platform/cf-tunnel token               -> cf-edge/cf-tunnel: token
#   <env>/chat-db, <env>/chat-pgbouncer    -> bravo-control/executor-pgpass: pgpass (only the fields that exist)
#   platform/cf-link-<env>-<svc>           -> bravo-link/cf-link-<env>-<svc>: client_id, client_secret (D-16 links)
#   platform/argocd-repo ssh_private_key   -> argocd/repo-b-free (Argo CD repository credential: read-only deploy key, SSH)
#   executor topology paths (deploy/cloud/in-cluster/topology.json) -> bravo-control/bx-<path>: every field that
#     exists, mounted at BRAVO_SECRETS_DIR/<path with / as __>/<field> (executor topology.FileSecrets)
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS="$HERE/../../pilot/scripts/keys.sh"
CTX="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
die() { printf 'deliver-secrets: %s\n' "$*" >&2; exit 1; }
log() { printf 'deliver-secrets: %s\n' "$*"; }

# One field from OpenBao as {"v": "<base64>"} on stdout; the value only crosses a pipe.
b64_field() { "$KEYS" env-pipe "$1" "$2" "$3" -- jq -Rs '{v: (. | @base64)}'; }

# secret NS NAME TYPE key=env/name/field ... [literal:key=value(non-secret)]
secret() {
  local ns=$1 name=$2 type=$3; shift 3
  local acc spec key src env sname field
  acc="$(jq -n --arg ns "$ns" --arg n "$name" --arg t "$type" \
    '{apiVersion: "v1", kind: "Secret", type: $t, metadata: {name: $n, namespace: $ns,
      labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}}, data: {}}')"
  for spec in "$@"; do
    if [[ $spec == literal:* ]]; then
      spec=${spec#literal:}; key=${spec%%=*}
      acc="$(printf '%s\n' "$acc" | jq --arg k "$key" --arg v "${spec#*=}" '.data[$k] = ($v | @base64)')"
      continue
    fi
    key=${spec%%=*}; src=${spec#*=}; IFS=/ read -r env sname field <<<"$src"
    # Merge without placing the value in argv: the second JSON document on stdin carries it.
    acc="$(printf '%s\n%s\n' "$acc" "$(b64_field "$env" "$sname" "$field")" | jq -s --arg k "$key" '.[0] as $s | .[1].v as $v | $s | .data[$k] = $v')"
  done
  kubectl --context "$CTX" get ns "$ns" >/dev/null 2>&1 || kubectl --context "$CTX" create ns "$ns" >/dev/null
  printf '%s\n' "$acc" | kubectl --context "$CTX" apply -f - >/dev/null
  log "Secret $ns/$name: keys $(printf '%s\n' "$acc" | jq -c '.data | keys') (values not shown)"
}

cmd_generate() {
  local f
  for f in owner api mcp executor apply; do "$KEYS" env-generate platform platform-db "$f" 24; done
  "$KEYS" env-generate platform api sse_ticket_secret 32
  "$KEYS" env-generate platform grafana admin_password 24
}

# libpq pgpass lines for the executor's chat DB / PgBouncer DSNs (executor.yaml). Built in memory.
pgpass_doc() {
  local out="" line
  # host:port:db:user:password ; host names are the bravo-link Services
  add() { # env name field host port db user
    local v
    if v="$("$KEYS" env-pipe "$1" "$2" "$3" -- cat 2>/dev/null)"; then
      out+="$4:$5:$6:$7:${v//:/\\:}"$'\n'
    else
      log "pgpass: secret/envs/$1/$2 field $3 absent -> $7@$4 has no password (connect fails: not connected)" >&2
    fi
  }
  add source-aws chat-db executor_ctl src-db.bravo-link.svc.cluster.local 5432 chat executor_ctl
  add source-aws chat-db app_writer src-db.bravo-link.svc.cluster.local 5432 chat app_writer
  add source-aws chat-db executor_observe src-db.bravo-link.svc.cluster.local 5432 chat executor_observe
  add target-onprem chat-db executor_ctl dst-db.bravo-link.svc.cluster.local 5432 chat executor_ctl
  add target-onprem chat-db app_writer dst-db.bravo-link.svc.cluster.local 5432 chat app_writer
  add target-onprem chat-db executor_observe dst-db.bravo-link.svc.cluster.local 5432 chat executor_observe
  add source-aws chat-pgbouncer app_writer src-pgb.bravo-link.svc.cluster.local 6432 chat app_writer
  add source-aws chat-pgbouncer pgb_admin src-pgb.bravo-link.svc.cluster.local 6432 pgbouncer pgb_admin
  add source-aws chat-pgbouncer pgb_stats src-pgb.bravo-link.svc.cluster.local 6432 pgbouncer pgb_stats
  printf '%s' "$out" | jq -Rs '{v: (. | @base64)}'
}

cmd_deliver() {
  secret bravo-control platform-db-owner kubernetes.io/basic-auth literal:username=bravo_owner password=platform/platform-db/owner
  secret bravo-control platform-db-roles Opaque api=platform/platform-db/api mcp=platform/platform-db/mcp executor=platform/platform-db/executor apply=platform/platform-db/apply
  secret bravo-control platform-api Opaque sse_ticket_secret=platform/api/sse_ticket_secret
  if [[ -n $("$KEYS" env-fields platform git-customer-repo) ]]; then
    secret bravo-control git-customer-repo kubernetes.io/ssh-auth ssh-privatekey=platform/git-customer-repo/private_key
  fi
  secret bravo-control platform-rehearsal Opaque api_token=platform/rehearsal/api_token token_key=platform/rehearsal/token_key
  secret bravo-observe grafana-admin Opaque admin_password=platform/grafana/admin_password
  secret cf-edge cf-tunnel Opaque token=platform/cf-tunnel/token
  local acc
  acc="$(jq -n '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: "executor-pgpass", namespace: "bravo-control",
    labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}}, data: {}}')"
  acc="$(printf '%s\n%s\n' "$acc" "$(pgpass_doc)" | jq -s '.[0] as $s | .[1].v as $v | $s | .data.pgpass = $v')"
  printf '%s\n' "$acc" | kubectl --context "$CTX" apply -f - >/dev/null
  log "Secret bravo-control/executor-pgpass: $(printf '%s\n' "$acc" | jq -r '.data.pgpass | @base64d | split("\n") | map(select(length > 0)) | length') entries (values not shown)"
  local p env name f specs
  for p in envs/source-aws/chat-db envs/target-onprem/chat-db envs/source-aws/chat-admin envs/target-onprem/chat-admin \
           envs/source-aws/object-store envs/target-onprem/object-store envs/target-onprem/k8s-executor; do
    env=${p#envs/}; name=${env#*/}; env=${env%%/*}; specs=()
    while read -r f; do [[ -n $f ]] && specs+=("$f=$env/$name/$f"); done < <("$KEYS" env-fields "$env" "$name")
    if ((${#specs[@]})); then secret bravo-control "bx-${p//\//-}" Opaque "${specs[@]}"
    else log "executor secret $p: not in OpenBao (topology entries that need it fail at executor start: not connected)"; fi
  done
  local l
  for l in src-k8s src-db src-pgb src-admin dst-k8s dst-db dst-pgb dst-admin; do
    secret bravo-link "cf-link-$l" Opaque "client_id=platform/cf-link-$l/client_id" "client_secret=platform/cf-link-$l/client_secret"
  done
  # Argo CD repository credential: read-only deploy key (SSH) for the private repo, kept only in OpenBao
  # secret/envs/platform/argocd-repo field ssh_private_key (GitHub deploy key id 168472040).
  if "$KEYS" env-fields platform argocd-repo | grep -qx ssh_private_key; then
    local repo
    repo="$("$KEYS" env-pipe platform argocd-repo ssh_private_key -- jq -Rs '{
      apiVersion: "v1", kind: "Secret", type: "Opaque",
      metadata: {name: "repo-b-free", namespace: "argocd",
        labels: {"argocd.argoproj.io/secret-type": "repository", "app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}},
      data: {type: ("git" | @base64), name: ("b-free" | @base64),
             url: ("git@github.com:mangowhoiscloud/b-free.git" | @base64),
             sshPrivateKey: ((if endswith("\n") then . else . + "\n" end) | @base64)}}')"
    printf '%s\n' "$repo" | kubectl --context "$CTX" apply -f - >/dev/null; repo=""
    log "Secret argocd/repo-b-free: ssh deploy key for git@github.com:mangowhoiscloud/b-free.git (value not shown)"
  else
    log "argocd repo credential: secret/envs/platform/argocd-repo (field ssh_private_key) is not in OpenBao; Argo CD cannot read the private repo yet"
  fi
  local ns
  for ns in bravo-control; do
    kubectl --context "$CTX" -n "$ns" create configmap openbao-ca --from-file=ca.crt="$CUSTODY_DIR/$(jq -r '.bao_target' "$CUSTODY_DIR/openbao-init.json")/ca.crt" \
      --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
  done
}

case ${1-} in
  generate) cmd_generate ;;
  deliver) cmd_deliver ;;
  *) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
