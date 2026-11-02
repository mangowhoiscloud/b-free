#!/usr/bin/env bash
# Platform credentials for the on-prem cluster (D-26: kept only in OpenBao; values move only through pipes).
#   onprem-tokens.sh [all|k8s|rgw-kms]      (default all)
#
# k8s: the on-prem cluster API, reached through the D-16 link dst-k8s.
#   collector: kube-system/bravo-collector (read-only ClusterRole)          -> secret/envs/target-onprem/collector-k8s
#   executor:  chat/bravo-executor (pods get/list, exec, portforward)        -> secret/envs/target-onprem/k8s-executor
#   rehearsal: kube-system/bravo-rehearsal (rehearsal-*/verify-* only, VAP)  -> secret/envs/target-onprem/k8s-rehearsal
# TokenRequest tokens, 168h; re-run before expiry (the old secret is deleted first: env-put never overwrites).
# Kubeconfig Secrets on the platform (server = link Service dst-k8s, tls-server-name kubernetes.default.svc, CA = the
# on-prem cluster CA, public): bravo-control/onprem-kubeconfig (collector), bravo-control/rehearsal-kubeconfig.
# The executor reads its token file from BRAVO_SECRETS_DIR (deliver-secrets.sh).
#
# rgw-kms: RGW SSE-KMS (OBJ-ENC). RGW wraps object data keys with transit key rgw-target-onprem in the platform OpenBao,
# reached through the D-16 link t1-plat-bao (deploy/landing-zone/overlays/rke2/rook/rgw-kms-link.yaml).
#   rook-ceph/cf-link-plat-bao  <- secret/envs/target-onprem/cf-link-plat-bao (client_id, client_secret;
#                                  keys.sh issue-service-token target-onprem dst-plat-bao cf-link-plat-bao)
#   rook-ceph/rgw-kms-token     <- secret/envs/target-onprem/rgw-kms token (keys.sh rgw-kms-issue: a NEW token each run,
#                                  TTL 1h, renewed every 15 min by CronJob rook-ceph/rgw-kms-renew, at most 168h)
#   rook-ceph/rgw-kms-ca        <- platform OpenBao CA (custody platform-rke2/ca.crt, public)
# RGW copies the token file when its pod starts, so this restarts RGW (only when CephObjectStore bravo has
# security.kms), waits for it, and then revokes the previous token (keys.sh rgw-kms-retire). Re-run before 168h.
# First time: run this, then apply objectstore.yaml (Rook checks that rgw-kms-token and rgw-kms-ca exist).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
KEYS="$REPO/deploy/pilot/scripts/keys.sh"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
PLATFORM="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
what=${1:-all}
case $what in all | k8s | rgw-kms) ;; *) echo "usage: onprem-tokens.sh [all|k8s|rgw-kms]" >&2; exit 2 ;; esac
ssa() { kubectl --context "$1" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null; }

k8s_tokens() {
  local ca
  kubectl --context "$ONPREM" apply -f "$HERE/../link/onprem-rbac/collector.yaml" -f "$HERE/../link/onprem-rbac/executor.yaml" \
    -f "$HERE/../link/onprem-rbac/rehearsal.yaml" >/dev/null
  ca="$(kubectl config view --raw -o json | jq -r --arg c "$ONPREM" \
    '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]')"
  [[ -n $ca && $ca != null ]] || { echo "onprem-tokens: no CA for context $ONPREM" >&2; exit 1; }
  mint() { # ns sa bao-name
    "$KEYS" env-delete target-onprem "$3" >/dev/null 2>&1 || true
    kubectl --context "$ONPREM" -n "$1" create token "$2" --duration=168h | tr -d '\n' | "$KEYS" env-put target-onprem "$3" token
  }
  kubeconfig_secret() { # bao-name secret-name user
    "$KEYS" env-pipe target-onprem "$1" token -- jq -Rs --arg ca "$ca" --arg n "$2" --arg u "$3" '{
      apiVersion: "v1", kind: "Secret", type: "Opaque",
      metadata: {name: $n, namespace: "bravo-control", labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}},
      data: {kubeconfig: ({apiVersion: "v1", kind: "Config", "current-context": "target-onprem",
        clusters: [{name: "target-onprem", cluster: {server: "https://dst-k8s.bravo-link.svc.cluster.local:443",
          "tls-server-name": "kubernetes.default.svc", "certificate-authority-data": $ca}}],
        users: [{name: $u, user: {token: .}}],
        contexts: [{name: "target-onprem", context: {cluster: "target-onprem", user: $u}}]} | tojson | @base64)}}' \
      | ssa "$PLATFORM"
    echo "onprem-tokens: Secret bravo-control/$2 from secret/envs/target-onprem/$1 (values not shown)"
  }
  mint kube-system bravo-collector collector-k8s
  mint chat bravo-executor k8s-executor
  mint kube-system bravo-rehearsal k8s-rehearsal
  kubeconfig_secret collector-k8s onprem-kubeconfig bravo-collector
  kubeconfig_secret k8s-rehearsal rehearsal-kubeconfig bravo-rehearsal
}

# secret_from_bao NS NAME BAO_NAME FIELD[=KEY]...: Secret NS/NAME in the on-prem cluster, one key per OpenBao field.
# Each value goes keys.sh env-pipe -> jq @base64 -> kubectl apply --server-side, on pipes only.
secret_from_bao() {
  local ns=$1 name=$2 bao=$3 spec f key s
  shift 3
  s="$(jq -nc --arg n "$name" --arg ns "$ns" '{apiVersion: "v1", kind: "Secret", type: "Opaque",
    metadata: {name: $n, namespace: $ns, labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}},
    data: {}}')"
  for spec in "$@"; do
    f=${spec%%=*}; key=${spec#*=}
    s="$(printf '%s\n%s\n' "$s" "$("$KEYS" env-pipe target-onprem "$bao" "$f" -- jq -Rs '{v: (. | @base64)}')" \
      | jq -s --arg k "$key" '.[0] as $s | .[1].v as $v | $s | .data[$k] = $v')"
  done
  printf '%s\n' "$s" | ssa "$ONPREM"
  s=""
  echo "onprem-tokens: Secret $ns/$name from secret/envs/target-onprem/$bao (values not shown)"
}

rgw_kms() {
  local ca="$CUSTODY_DIR/platform-rke2/ca.crt"
  k() { kubectl --context "$ONPREM" "$@"; }
  [[ -f $ca ]] || { echo "onprem-tokens: no platform OpenBao CA at $ca" >&2; exit 1; }
  secret_from_bao rook-ceph cf-link-plat-bao cf-link-plat-bao client_id client_secret
  k -n rook-ceph create secret generic rgw-kms-ca --from-file=cert="$ca" --dry-run=client -o json \
    | jq '.metadata.labels = {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "custody-public-ca"}' | ssa "$ONPREM"
  echo "onprem-tokens: Secret rook-ceph/rgw-kms-ca (platform OpenBao CA, public)"
  k apply -f "$REPO/deploy/landing-zone/overlays/rke2/rook/rgw-kms-link.yaml" >/dev/null
  k -n rook-ceph rollout restart deploy/plat-bao-link >/dev/null   # picks up a rotated service token
  k -n rook-ceph rollout status deploy/plat-bao-link --timeout=120s
  "$KEYS" rgw-kms-issue target-onprem
  secret_from_bao rook-ceph rgw-kms-token rgw-kms token
  if [[ -n $(k -n rook-ceph get cephobjectstore bravo -o jsonpath='{.spec.security.kms.tokenSecretName}' 2>/dev/null) ]]; then
    # Deleting the pod (not rollout restart) keeps the Rook-owned Deployment spec unchanged.
    k -n rook-ceph delete pod -l app=rook-ceph-rgw,rook_object_store=bravo --wait=true >/dev/null
    k -n rook-ceph rollout status deploy/rook-ceph-rgw-bravo-a --timeout=300s
    k -n rook-ceph wait --for=condition=Ready pod -l app=rook-ceph-rgw,rook_object_store=bravo --timeout=300s >/dev/null
    echo "onprem-tokens: RGW restarted with the new token"
  else
    echo "onprem-tokens: CephObjectStore bravo has no security.kms yet; apply objectstore.yaml next (RGW then starts with this token)"
  fi
  "$KEYS" rgw-kms-retire target-onprem
}

[[ $what == rgw-kms ]] || k8s_tokens
[[ $what == k8s ]] || rgw_kms
