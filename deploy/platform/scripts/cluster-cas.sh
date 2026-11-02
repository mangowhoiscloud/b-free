#!/usr/bin/env bash
# Public cluster CA certificates for the executor kubeconfig (ConfigMap bravo-control/executor-kube-ca):
#   source-ca.crt: EKS bravo-source (eks:DescribeCluster), onprem-ca.crt: the on-prem cluster CA (kubeconfig context).
# Not secret. Re-run when a cluster is re-created.
set -euo pipefail
PLATFORM="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
src="$(aws eks describe-cluster --name bravo-source --region ap-northeast-2 --query cluster.certificateAuthority.data --output text | base64 -d)"
onp="$(kubectl config view --raw -o json | jq -r --arg c "$ONPREM" \
  '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]' | base64 -d)"
kubectl --context "$PLATFORM" -n bravo-control create configmap executor-kube-ca \
  --from-literal=source-ca.crt="$src" --from-literal=onprem-ca.crt="$onp" --dry-run=client -o yaml \
  | kubectl --context "$PLATFORM" apply -f - >/dev/null
printf 'cluster-cas: source %s; onprem %s\n' "$(printf '%s' "$src" | openssl x509 -noout -subject)" "$(printf '%s' "$onp" | openssl x509 -noout -subject)"
