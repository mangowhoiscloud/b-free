#!/usr/bin/env bash
# Collector access to the on-prem K3s through the D-16 link dst-k8s (D-26: the credential lives only in the
# platform OpenBao). 1) TokenRequest (168h) for kube-system/bravo-collector on bravo-onprem -> OpenBao
# secret/envs/target-onprem/collector-k8s (field token). 2) kubeconfig Secret bravo-control/onprem-kubeconfig on
# the platform: server = link Service dst-k8s, tls-server-name kubernetes.default.svc (K3s cert SAN), CA = the
# K3s cluster CA (public). The token moves only through pipes. Re-run before the 168h expiry.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS="$HERE/../../pilot/scripts/keys.sh"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
PLATFORM="${BRAVO_PLATFORM_CONTEXT:-bravo-platform}"
kubectl --context "$ONPREM" apply -f "$HERE/../link/onprem-rbac/collector.yaml" >/dev/null
kubectl --context "$ONPREM" -n kube-system create token bravo-collector --duration=168h | tr -d '\n' \
  | "$KEYS" env-put target-onprem collector-k8s token
CA="$(kubectl config view --raw -o json | jq -r --arg c "$ONPREM" \
  '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]')"
[[ -n $CA && $CA != null ]] || { echo "onprem-collector-token: no CA for context $ONPREM" >&2; exit 1; }
"$KEYS" env-pipe target-onprem collector-k8s token -- jq -Rs --arg ca "$CA" '{
  apiVersion: "v1", kind: "Secret", type: "Opaque",
  metadata: {name: "onprem-kubeconfig", namespace: "bravo-control",
             labels: {"app.kubernetes.io/managed-by": "bravo-keys", "bravo.io/source": "openbao"}},
  data: {kubeconfig: ({apiVersion: "v1", kind: "Config", "current-context": "target-onprem",
    clusters: [{name: "target-onprem", cluster: {server: "https://dst-k8s.bravo-link.svc.cluster.local:443",
      "tls-server-name": "kubernetes.default.svc", "certificate-authority-data": $ca}}],
    users: [{name: "bravo-collector", user: {token: .}}],
    contexts: [{name: "target-onprem", context: {cluster: "target-onprem", user: "bravo-collector"}}]} | tojson | @base64)}}' \
  | kubectl --context "$PLATFORM" apply -f - >/dev/null
echo "onprem-collector-token: token in OpenBao secret/envs/target-onprem/collector-k8s, Secret bravo-control/onprem-kubeconfig updated (values not shown)"
