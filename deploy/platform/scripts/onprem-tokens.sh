#!/usr/bin/env bash
# Platform credentials for the on-prem cluster API, reached through the D-16 link dst-k8s (D-26: kept only in OpenBao).
#   collector: kube-system/bravo-collector (read-only ClusterRole)          -> secret/envs/target-onprem/collector-k8s
#   executor:  chat/bravo-executor (pods get/list, exec, portforward)        -> secret/envs/target-onprem/k8s-executor
#   rehearsal: kube-system/bravo-rehearsal (rehearsal-*/verify-* only, VAP)  -> secret/envs/target-onprem/k8s-rehearsal
# TokenRequest tokens, 168h; re-run before expiry (the old secret is deleted first: env-put never overwrites).
# Kubeconfig Secrets on the platform (server = link Service dst-k8s, tls-server-name kubernetes.default.svc, CA = the
# on-prem cluster CA, public): bravo-control/onprem-kubeconfig (collector), bravo-control/rehearsal-kubeconfig.
# The executor reads its token file from BRAVO_SECRETS_DIR (deliver-secrets.sh). Values move only through pipes.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS="$HERE/../../pilot/scripts/keys.sh"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
PLATFORM="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
kubectl --context "$ONPREM" apply -f "$HERE/../link/onprem-rbac/collector.yaml" -f "$HERE/../link/onprem-rbac/executor.yaml" \
  -f "$HERE/../link/onprem-rbac/rehearsal.yaml" >/dev/null
CA="$(kubectl config view --raw -o json | jq -r --arg c "$ONPREM" \
  '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]')"
[[ -n $CA && $CA != null ]] || { echo "onprem-tokens: no CA for context $ONPREM" >&2; exit 1; }

mint() { # ns sa bao-name
  "$KEYS" env-delete target-onprem "$3" >/dev/null 2>&1 || true
  kubectl --context "$ONPREM" -n "$1" create token "$2" --duration=168h | tr -d '\n' | "$KEYS" env-put target-onprem "$3" token
}
kubeconfig_secret() { # bao-name secret-name user
  "$KEYS" env-pipe target-onprem "$1" token -- jq -Rs --arg ca "$CA" --arg n "$2" --arg u "$3" '{
    apiVersion: "v1", kind: "Secret", type: "Opaque",
    metadata: {name: $n, namespace: "bravo-control", labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}},
    data: {kubeconfig: ({apiVersion: "v1", kind: "Config", "current-context": "target-onprem",
      clusters: [{name: "target-onprem", cluster: {server: "https://dst-k8s.bravo-link.svc.cluster.local:443",
        "tls-server-name": "kubernetes.default.svc", "certificate-authority-data": $ca}}],
      users: [{name: $u, user: {token: .}}],
      contexts: [{name: "target-onprem", context: {cluster: "target-onprem", user: $u}}]} | tojson | @base64)}}' \
    | kubectl --context "$PLATFORM" apply -f - >/dev/null
  echo "onprem-tokens: Secret bravo-control/$2 from secret/envs/target-onprem/$1 (values not shown)"
}
mint kube-system bravo-collector collector-k8s
mint chat bravo-executor k8s-executor
mint kube-system bravo-rehearsal k8s-rehearsal
kubeconfig_secret collector-k8s onprem-kubeconfig bravo-collector
kubeconfig_secret k8s-rehearsal rehearsal-kubeconfig bravo-rehearsal
