#!/usr/bin/env bash
# Read-only snapshot for the rke2-traefik-crd skip change (before / after): server manifests directory,
# Traefik HelmChart · Addon · Job · Pod, Helm release records, Gateway API CRDs and Gateway / HTTPRoute objects
# (uid, resourceVersion, generation), and the public routes. KST times.
#   state.sh <label>
# Clusters: bravo-onprem (Lima server VM bravo-onprem-server) and bravo-platform-rke2 (EC2 via SSM).
set -uo pipefail
PLATFORM_IID=${BRAVO_PLATFORM_IID:-i-08c0b121d702dca59}
REGION=${AWS_REGION:-ap-northeast-2}
M=/var/lib/rancher/rke2/server/manifests

ts() { TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST'; }
ssm() {  # run one read-only shell command on the platform node, print its output
  local id st
  id=$(aws ssm send-command --region "$REGION" --instance-ids "$PLATFORM_IID" --document-name AWS-RunShellScript \
    --parameters "$(jq -nc --arg c "$1" '{commands:[$c]}')" --query Command.CommandId --output text)
  for _ in $(seq 1 60); do
    st=$(aws ssm get-command-invocation --region "$REGION" --command-id "$id" --instance-id "$PLATFORM_IID" --query Status --output text 2>/dev/null || echo Pending)
    case $st in Success|Failed|Cancelled|TimedOut) break ;; esac; sleep 2
  done
  aws ssm get-command-invocation --region "$REGION" --command-id "$id" --instance-id "$PLATFORM_IID" --query StandardOutputContent --output text
}
node_ls() {
  case $1 in
    bravo-onprem) limactl shell bravo-onprem-server -- sudo ls -l --time-style=+%FT%T%z "$M" </dev/null ;;
    bravo-platform-rke2) ssm "ls -l --time-style=+%FT%T%z $M" ;;
  esac
}

printf '# state %s  %s\n' "${1:?label}" "$(ts)"
for c in bravo-onprem bravo-platform-rke2; do
  printf '\n## %s\n' "$c"
  echo "### server manifests ($M)"; node_ls "$c" | awk '{print $6, $7}' | sed '/^ *$/d'
  echo "### Traefik HelmChart, Addon, Job, Pod"
  kubectl --context "$c" -n kube-system get helmcharts.helm.cattle.io -o name | grep -i traefik || echo "HelmChart: none"
  kubectl --context "$c" -n kube-system get addons.k3s.cattle.io -o name | grep -i traefik || echo "Addon: none"
  kubectl --context "$c" -n kube-system get jobs -o name | grep -i traefik || echo "Job: none"
  kubectl --context "$c" -n kube-system get pods --no-headers | grep -i traefik || echo "Pod: none"
  echo "### Helm release records (owner=helm) named *traefik*"
  kubectl --context "$c" -n kube-system get secrets -l owner=helm -o name | grep -i traefik || echo "none"
  echo "### Gateway API CRDs (name uid resourceVersion created)"
  kubectl --context "$c" get crd -o custom-columns=N:.metadata.name,U:.metadata.uid,RV:.metadata.resourceVersion,C:.metadata.creationTimestamp --no-headers | grep 'gateway.networking.k8s.io'
  echo "### Gateways and HTTPRoutes (kind ns name uid resourceVersion generation)"
  kubectl --context "$c" get gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io -A \
    -o custom-columns=K:.kind,NS:.metadata.namespace,N:.metadata.name,U:.metadata.uid,RV:.metadata.resourceVersion,G:.metadata.generation --no-headers 2>&1 || true
  kubectl --context "$c" get gateways.gateway.networking.k8s.io -A -o custom-columns=NS:.metadata.namespace,N:.metadata.name,PROGRAMMED:'.status.conditions[?(@.type=="Programmed")].status' --no-headers 2>/dev/null || true
done

printf '\n## public routes (HTTP status, time)\n'
for u in https://b-free.mangowhois.ai/ https://b-free.mangowhois.ai/healthz https://b-free-chat.mangowhois.ai/api/config \
         https://b-free-chat-dst.mangowhois.ai/api/config https://b-free-chat-src.mangowhois.ai/api/config; do
  printf '%s  %s\n' "$(curl -s -o /dev/null -m 15 -w '%{http_code} %{time_total}s' "$u")" "$u"
done
