# Shared helpers for deploy/scripts/preflight.sh, rotate-credentials.sh and demo-freeze.sh (sourced, not run).
# Never prints a secret value. Contexts follow docs/spec and deploy/CLOUD-E2E-CONTRACT.md 2:
#   platform  bravo-platform-rke2 (EC2 + RKE2, SSM port-forward 127.0.0.1:26443)
#   source    bravo-source (EKS)      onprem  bravo-onprem (RKE2 on Lima)
#   gke       bravo-target-gcp from the kubeconfig that infra/envs/target-gcp writes (scripts/lib.sh, gke-gcloud-auth-plugin)
DEMO_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DEMO_LIB_DIR/../.." && pwd)"
KEYS="$REPO/deploy/pilot/scripts/keys.sh"
PLATFORM_CTX="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
SOURCE_CTX="${BRAVO_SOURCE_CONTEXT:-bravo-source}"
ONPREM_CTX="${BRAVO_ONPREM_CONTEXT:-bravo-onprem}"
GKE_CTX="${BRAVO_GKE_CONTEXT:-bravo-target-gcp}"
GKE_KUBECONFIG="${BRAVO_GKE_KUBECONFIG:-$REPO/infra/envs/target-gcp/.terraform/bravo/kubeconfig}"
# gke-gcloud-auth-plugin sits next to gcloud; infra/envs/target-gcp/scripts/lib.sh finds it the same way.
if [[ -z ${DEMO_GCLOUD_BIN-} ]]; then
  DEMO_GCLOUD_BIN="$(bash -c 'source "$1" >/dev/null 2>&1 && printf %s "$GCLOUD_BIN"' _ \
    "$REPO/infra/envs/target-gcp/scripts/lib.sh" 2>/dev/null || true)"
  [[ -n $DEMO_GCLOUD_BIN ]] && export PATH="$DEMO_GCLOUD_BIN:$PATH"
  export DEMO_GCLOUD_BIN USE_GKE_GCLOUD_AUTH_PLUGIN=True
fi

# kc ALIAS kubectl-args...  (ALIAS: platform | source | onprem | gke)
kc() {
  local a=$1; shift
  case $a in
    platform) kubectl --context "$PLATFORM_CTX" --request-timeout=20s "$@" ;;
    source) kubectl --context "$SOURCE_CTX" --request-timeout=20s "$@" ;;
    onprem) kubectl --context "$ONPREM_CTX" --request-timeout=20s "$@" ;;
    gke) kubectl --kubeconfig "$GKE_KUBECONFIG" --context "$GKE_CTX" --request-timeout=20s "$@" ;;
    *) echo "kc: unknown alias $a" >&2; return 2 ;;
  esac
}

free_port() { python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
kst_now() { TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST'; }

# D-16 link clients (cloudflared access tcp): ALIAS NS DEPLOYMENT PORT OPENBAO_ENV OPENBAO_NAME. The Secret in NS has the
# same name as OPENBAO_NAME (client_id, client_secret). Measured 2026-11-03 02:3x KST with kubectl (20 clients).
DEMO_LINKS=(
  "platform bravo-link link-src-k8s 6443 platform cf-link-src-k8s"
  "platform bravo-link link-src-db 5432 platform cf-link-src-db"
  "platform bravo-link link-src-pgb 6432 platform cf-link-src-pgb"
  "platform bravo-link link-src-admin 8081 platform cf-link-src-admin"
  "platform bravo-link link-dst-k8s 6443 platform cf-link-dst-k8s"
  "platform bravo-link link-dst-db 5432 platform cf-link-dst-db"
  "platform bravo-link link-dst-pgb 6432 platform cf-link-dst-pgb"
  "platform bravo-link link-dst-admin 8081 platform cf-link-dst-admin"
  "platform bravo-link link-dst-s3 8443 platform cf-link-dst-s3"
  "platform bravo-link link-dst-s3http 8080 platform cf-link-dst-s3http"
  "platform bravo-link link-gke-db 5432 platform cf-link-gke-db"
  "platform bravo-link link-gke-k8s 6443 platform cf-link-gke-k8s"
  "source chat dst-db-link 5432 source-aws cf-link-dst-db"
  "source bravo-observability platform-otel 4317 source-aws cf-link-plat-otel"
  "onprem chat src-db-link 5432 target-onprem cf-link-src-db"
  "onprem rook-ceph plat-bao-link 8200 target-onprem cf-link-plat-bao"
  "onprem bravo-observability platform-otel 4317 target-onprem cf-link-plat-otel"
  "gke chat src-db-link 5432 target-gcp cf-link-src-db"
  "gke external-secrets openbao 8200 target-gcp cf-link-plat-bao"
  "gke bravo-observability platform-otel 4317 target-gcp cf-link-plat-otel"
)

# link_probe ALIAS NS DEPLOYMENT PORT -> one line "OK|FAIL <detail> <ms>ms".
# A private port-forward into the link client Pod, then one request in the protocol of the far end. Any answer from
# the far end (Postgres accepting connections, an HTTP status from the API server, RGW, OpenBao, the OTel collector)
# proves client -> Cloudflare (service token) -> tunnel -> service. The cloudflared listener alone does not answer.
link_probe() {
  local a=$1 ns=$2 dep=$3 port=$4 lp pf i t0 t1 outp rc=0
  lp="$(free_port)"
  kc "$a" -n "$ns" port-forward "deploy/$dep" "$lp:$port" >/dev/null 2>&1 &
  pf=$!
  for i in $(seq 1 60); do nc -z 127.0.0.1 "$lp" 2>/dev/null && break; kill -0 "$pf" 2>/dev/null || break; sleep 0.25; done
  t0="$(python3 -I -c 'import time;print(int(time.time()*1000))')"
  case $port in
    5432 | 6432) outp="$(pg_isready -h 127.0.0.1 -p "$lp" -t 15 2>&1)" || rc=1 ;;
    6443 | 443) outp="HTTP $(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:$lp/readyz")" ;;
    8443) outp="HTTP $(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:$lp/")" ;;
    8200) outp="HTTP $(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:$lp/v1/sys/health")" ;;
    4317) outp="HTTP $(curl -s -o /dev/null -w '%{http_code}' --http2-prior-knowledge --max-time 15 "http://127.0.0.1:$lp/")" ;;
    *) outp="HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://127.0.0.1:$lp/")" ;;
  esac
  t1="$(python3 -I -c 'import time;print(int(time.time()*1000))')"
  kill "$pf" 2>/dev/null; wait "$pf" 2>/dev/null
  [[ $outp == "HTTP 000" || $outp == *"no response"* || -z $outp ]] && rc=1
  [[ $port == 8200 && $outp != "HTTP 200" ]] && rc=1   # OpenBao sealed (503) or standby is not usable
  outp="${outp//127.0.0.1:$lp - /}"
  printf '%s %s %sms\n' "$([[ $rc == 0 ]] && echo OK || echo FAIL)" "$outp" "$((t1 - t0))"
}
