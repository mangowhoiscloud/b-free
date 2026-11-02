#!/usr/bin/env bash
# DB CA bundle (ENC-01, gate DB-TLS): the public CNPG CA certificate of every chat-db (source EKS, on-prem RKE2,
# GKE) in ConfigMap chat/bravo-db-ca of every cluster. Every DB client verifies the server it reaches, in the same
# cluster or over a D-16 link, with sslmode=verify-full against this file:
#   ca.crt     the CAs in one file. Read by PgBouncer (server_tls_ca_file: its target moves between the local
#              chat-db and the other environment's chat-db at cutover), the demo app (sslrootcert, PgBouncer serves
#              the local chat-db certificate) and the CNPG subscriber (/projected/db-ca/ca.crt).
#   <env>.crt  one CA each, for diagnostics (openssl verify -CAfile).
# Certificates only, never a key. A CNPG CA is valid 90 days; re-run after a chat-db is re-created or its CA is
# renewed, then roll chat-pgbouncer (it reads the file at start). libpq clients read it at every new connection.
#   db-ca.sh [apply|show]          default apply
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
SOURCE="${BRAVO_SOURCE_CONTEXT:-bravo-source}"
ONPREM="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
GKE_KUBECONFIG="${BRAVO_GKE_KUBECONFIG:-$repo/infra/envs/target-gcp/.terraform/bravo/kubeconfig}"
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"   # gke-gcloud-auth-plugin
k() { local ctx=$1; shift; if [[ $ctx == gke ]]; then KUBECONFIG="$GKE_KUBECONFIG" kubectl --context bravo-target-gcp "$@"; else kubectl --context "$ctx" "$@"; fi; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
ctxs=()
for pair in "source-aws:$SOURCE" "target-onprem:$ONPREM" "target-gcp:gke"; do
  env=${pair%%:*}; ctx=${pair#*:}
  # A cluster that is down or has no chat-db yet is left out (warned), so a fresh install can run this per cluster.
  if ! k "$ctx" -n chat get secret chat-db-ca -o jsonpath='{.data.ca\.crt}' 2>/dev/null | base64 -d >"$tmp/$env.crt" \
    || ! openssl x509 -in "$tmp/$env.crt" -noout >/dev/null 2>&1; then
    echo "db-ca: WARNING $env ($ctx): no chat-db CA, left out of the bundle" >&2; rm -f "$tmp/$env.crt"; continue
  fi
  ctxs+=("$ctx")
done
((${#ctxs[@]})) || { echo "db-ca: no chat-db CA in any cluster" >&2; exit 1; }
for f in "$tmp"/*.crt; do
  printf '%-18s %s, notAfter %s\n' "$(basename "$f")" "$(openssl x509 -in "$f" -noout -fingerprint -sha256 | cut -c1-40)…" \
    "$(openssl x509 -in "$f" -noout -enddate | cut -d= -f2)"
done
cat "$tmp"/*.crt >"$tmp/bundle" && mv "$tmp/bundle" "$tmp/ca.crt"
echo "ca.crt             $(grep -c 'BEGIN CERTIFICATE' "$tmp/ca.crt") certificates"
[[ ${1:-apply} == show ]] && exit 0
for ctx in "${ctxs[@]}"; do
  k "$ctx" -n chat create configmap bravo-db-ca --from-file="$tmp" --dry-run=client -o json \
    | jq '.metadata.labels = {"app.kubernetes.io/part-of": "bravo-landing-zone", "bravo.dev/content": "public-ca-certificates"}' \
    | k "$ctx" apply --server-side --force-conflicts --field-manager=bravo-db-ca -f - >/dev/null
  echo "ConfigMap chat/bravo-db-ca on $ctx: ca.crt ($(grep -c "BEGIN CERTIFICATE" "$tmp/ca.crt") CNPG CAs) + one file per environment"
done
