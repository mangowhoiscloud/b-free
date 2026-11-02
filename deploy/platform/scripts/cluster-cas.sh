#!/usr/bin/env bash
# Public cluster CA certificates for the executor kubeconfig (ConfigMap bravo-control/executor-kube-ca):
#   source-ca.crt: EKS bravo-source (eks:DescribeCluster), onprem-ca.crt: the on-prem cluster CA (kubeconfig context).
# and the CNPG CA of each chat-db the Executor reaches over the D-16 links (ENC-01, verify-full in
# deploy/cloud/in-cluster/topology.json; the pod sees them at /etc/bravo/kube/<key>):
#   source-aws-chat-db.crt, target-onprem-chat-db.crt: Secret chat/chat-db-ca ca.crt (public part only). PgBouncer
#   serves the same chat-db certificate, so the pool connections use the same file.
# Not secret. Re-run when a cluster or its chat-db is re-created.
set -euo pipefail
PLATFORM="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
src="$(aws eks describe-cluster --name bravo-source --region ap-northeast-2 --query cluster.certificateAuthority.data --output text | base64 -d)"
onp="$(kubectl config view --raw -o json | jq -r --arg c "$ONPREM" \
  '(.contexts[] | select(.name == $c) | .context.cluster) as $cl | .clusters[] | select(.name == $cl) | .cluster["certificate-authority-data"]' | base64 -d)"
SOURCE="${BRAVO_SOURCE_CONTEXT:-bravo-source}"
src_db="$(kubectl --context "$SOURCE" -n chat get secret chat-db-ca -o jsonpath='{.data.ca\.crt}' | base64 -d)"
onp_db="$(kubectl --context "$ONPREM" -n chat get secret chat-db-ca -o jsonpath='{.data.ca\.crt}' | base64 -d)"
for c in "$src_db" "$onp_db"; do printf '%s\n' "$c" | openssl x509 -noout >/dev/null || { echo "cluster-cas: chat-db CA is not a certificate" >&2; exit 1; }; done
kubectl --context "$PLATFORM" -n bravo-control create configmap executor-kube-ca \
  --from-literal=source-ca.crt="$src" --from-literal=onprem-ca.crt="$onp" \
  --from-literal=source-aws-chat-db.crt="$src_db" --from-literal=target-onprem-chat-db.crt="$onp_db" --dry-run=client -o yaml \
  | kubectl --context "$PLATFORM" apply -f - >/dev/null
printf 'cluster-cas: source %s; onprem %s\n' "$(printf '%s' "$src" | openssl x509 -noout -subject)" "$(printf '%s' "$onp" | openssl x509 -noout -subject)"
printf 'cluster-cas: chat-db source %s; chat-db onprem %s\n' "$(printf '%s\n' "$src_db" | openssl x509 -noout -subject)" \
  "$(printf '%s\n' "$onp_db" | openssl x509 -noout -subject)"
