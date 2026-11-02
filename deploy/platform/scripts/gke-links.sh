#!/usr/bin/env bash
# GKE target links on the D-16 pattern (deploy/platform/link/gke-consumer): service-token Secrets piped from OpenBao,
# the cloudflared access tcp Deployments replacing the laptop relays, and the ESO CA bundle = platform OpenBao CA.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYS="$HERE/../../pilot/scripts/keys.sh"
TF="$(cd "$HERE/../../../infra/envs/target-gcp" && pwd)"
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"
G=(kubectl --kubeconfig "$(terraform -chdir="$TF" output -raw kubeconfig_path)" --context "$(terraform -chdir="$TF" output -raw cluster_name)")
for spec in "external-secrets cf-link-plat-bao" "bravo-observability cf-link-plat-otel" "chat cf-link-src-db"; do
  read -r ns sec <<<"$spec"
  acc="$(jq -n --arg ns "$ns" --arg n "$sec" '{apiVersion:"v1",kind:"Secret",type:"Opaque",metadata:{name:$n,namespace:$ns,labels:{"bravo.io/source":"openbao"}},data:{}}')"
  for f in client_id client_secret; do
    acc="$(printf '%s\n%s\n' "$acc" "$("$KEYS" env-pipe target-gcp "$sec" "$f" -- jq -Rs '{v: (. | @base64)}')" | jq -s --arg k "$f" '.[0] as $s | .[1].v as $v | $s | .data[$k] = $v')"
  done
  printf '%s\n' "$acc" | "${G[@]}" apply --server-side --force-conflicts --field-manager=bravo-secrets -f - >/dev/null; echo "gke-links: Secret $ns/$sec (values not shown)"
done
# The relays rendered by links.sh carry a ConfigMap and a different pod template; replace the Deployments.
for spec in "external-secrets openbao" "bravo-observability platform-otel" "chat src-db-link"; do
  read -r ns name <<<"$spec"
  "${G[@]}" -n "$ns" delete deploy "$name" --ignore-not-found >/dev/null
  "${G[@]}" -n "$ns" delete configmap "$name-relay" --ignore-not-found >/dev/null
done
"${G[@]}" apply -k "$HERE/../link/gke-consumer" >/dev/null
CA="$(base64 < "${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}/$(jq -r .bao_target "${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}/openbao-init.json")/ca.crt" | tr -d '\n')"
"${G[@]}" patch clustersecretstore bravo-platform-openbao --type merge -p "{\"spec\":{\"provider\":{\"vault\":{\"caBundle\":\"$CA\"}}}}" >/dev/null
echo "gke-links: ClusterSecretStore bravo-platform-openbao caBundle = platform OpenBao CA (public)"
for spec in "external-secrets openbao" "bravo-observability platform-otel" "chat src-db-link"; do
  read -r ns name <<<"$spec"; "${G[@]}" -n "$ns" rollout status deploy/"$name" --timeout=120s | tail -1
done
