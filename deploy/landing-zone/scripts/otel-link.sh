#!/usr/bin/env bash
# OTel agent + D-16 link to the platform Collector on one landing-zone cluster (07 6, 15-r2-contracts 7).
#   otel-link.sh <overlay eks|rke2> <kube-context> <env-id source-aws|target-onprem>
# 1. Secret bravo-observability/cf-link-plat-otel (client_id, client_secret) from OpenBao
#    secret/envs/<env-id>/cf-link-plat-otel, on a pipe only (D-26). The token is issued by
#    keys.sh issue-service-token <env-id> <src|dst>-plat-otel cf-link-plat-otel and listed in
#    infra/envs/edge terraform.tfvars platform_links.otel.token_ids.
# 2. Link client (deploy/landing-zone/observability): Service platform-otel:4317 -> t1-plat-otel.
# 3. Helm release otel-agent: base/otel-agent-values.yaml + overlays/<overlay>/otel-agent-env.yaml.
# GKE has its own copy (deploy/platform/scripts/gke-links.sh, deploy/platform/link/gke-consumer).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$here/../.." && pwd)"
keys="$repo/deploy/pilot/scripts/keys.sh"
overlay=${1:?usage: otel-link.sh <overlay> <kube-context> <env-id>}
ctx=${2:?usage: otel-link.sh <overlay> <kube-context> <env-id>}
env_id=${3:?usage: otel-link.sh <overlay> <kube-context> <env-id>}
[[ -f $here/overlays/$overlay/otel-agent-env.yaml ]] || { echo "otel-link.sh: no overlays/$overlay/otel-agent-env.yaml" >&2; exit 1; }
# shellcheck source=../versions.env
set -a; . "$here/versions.env"; set +a
k() { kubectl --context "$ctx" "$@"; }

k get ns bravo-observability >/dev/null
secret='{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"cf-link-plat-otel","namespace":"bravo-observability","labels":{"bravo.io/source":"openbao"}},"data":{}}'
for f in client_id client_secret; do
  secret="$(printf '%s\n%s\n' "$secret" "$("$keys" env-pipe "$env_id" cf-link-plat-otel "$f" -- jq -Rs '{v: (. | @base64)}')" \
    | jq -s --arg k "$f" '.[0] as $s | .[1].v as $v | $s | .data[$k] = $v')"
done
printf '%s\n' "$secret" | k apply -f - >/dev/null
secret=""
echo "otel-link: Secret bravo-observability/cf-link-plat-otel from secret/envs/$env_id/cf-link-plat-otel (values not shown)"

k apply -k "$here/observability" >/dev/null
k -n bravo-observability rollout restart deploy/platform-otel >/dev/null   # picks up a rotated token
k -n bravo-observability rollout status deploy/platform-otel --timeout=120s

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm --kube-context "$ctx" upgrade --install otel-agent open-telemetry/opentelemetry-collector --version "$OTEL_CHART_VERSION" \
  -n bravo-observability -f "$here/base/otel-agent-values.yaml" -f "$here/overlays/$overlay/otel-agent-env.yaml" \
  --set image.repository="$OTEL_IMAGE_REPO" --set image.digest="$OTEL_IMAGE_DIGEST" --wait --timeout 5m >/dev/null
k -n bravo-observability rollout status deploy/otel-agent --timeout=180s
echo "otel-link: otel-agent (bravo.env=$env_id) -> platform-otel -> t1-plat-otel"
