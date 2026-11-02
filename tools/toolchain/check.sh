#!/usr/bin/env bash
# 클라우드 작업 전에 도구와 판을 확인합니다. 하나라도 없거나 최소 판보다 낮으면 실패합니다.
set -uo pipefail
fail=0
need() {  # 이름 최소판 판을_출력하는_명령...
  local name=$1 min=$2; shift 2
  if ! command -v "$1" >/dev/null 2>&1; then echo "없음   $name"; fail=1; return; fi
  local got; got=$("$@" 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
  if [ -z "$got" ] || [ "$(printf '%s\n%s\n' "$min" "$got" | sort -V | head -1)" != "$min" ]; then
    echo "낮음   $name $got < $min"; fail=1
  else echo "확인   $name $got"; fi
}
LIBPQ_BIN="${LIBPQ_BIN:-$(brew --prefix libpq 2>/dev/null)/bin}"
# gcloud 구성요소는 SDK의 bin에 설치됩니다. PATH에 없으면 그 위치를 더합니다.
GCLOUD_BIN="${GCLOUD_BIN:-$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null)/bin}"
case ":$PATH:" in *":$GCLOUD_BIN:"*) ;; *) PATH="$PATH:$GCLOUD_BIN";; esac
need terraform 1.17 terraform version
need kubectl 1.34 kubectl version --client
need helm 4.0 helm version --short
need argocd 3.2 argocd version --client --short
need kyverno 1.19 kyverno version
need kubeconform 0.8 kubeconform -v
need cosign 3.1 cosign version
need syft 1.55 syft version
need trivy 0.76 trivy --version
need gitleaks 8.30 gitleaks version
need yq 4.54 yq --version
need jq 1.7 jq --version
need gh 2.82 gh --version
need aws 2.31 aws --version
need gcloud 587.0 gcloud --version
need gke-gcloud-auth-plugin 0.0 gke-gcloud-auth-plugin --version
need session-manager-plugin 1.2 session-manager-plugin --version
need cloudflared 2026.10 cloudflared --version
need limactl 1.2 limactl --version
need docker 29.0 docker --version
need uv 0.10 uv --version
need infracost 2.18 infracost --version
need psql 18.0 "$LIBPQ_BIN/psql" --version
exit $fail
