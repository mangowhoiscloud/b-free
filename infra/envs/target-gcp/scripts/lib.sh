# Shared helpers for infra/envs/target-gcp/scripts (sourced). Never prints a secret value.
# Every gcloud call passes --project explicitly; the gcloud default configuration is not used or changed.
set -euo pipefail
umask 077

TG_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TG_ENV_DIR="$(cd "$TG_SCRIPTS/.." && pwd)"
REPO_ROOT="$(cd "$TG_ENV_DIR/../../.." && pwd)"
OVERLAY_DIR="$REPO_ROOT/deploy/landing-zone/overlays/gke"
PILOT_DIR="$REPO_ROOT/deploy/pilot"
BAO_ENV="target-gcp"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
CUSTODY="$CUSTODY_DIR/openbao-init.json"
# Platform OpenBao target, same rule as deploy/pilot/scripts/keys.sh: custody `bao_target` says where the one
# store lives (D-26). platform-eks / platform-rke2: reached only by kubectl port-forward to openbao/openbao-0
# with the CA in $CUSTODY_DIR/<target>/ca.crt. pilot: the laptop Compose store on 127.0.0.1:8200.
BAO_TARGET="${BRAVO_BAO_TARGET:-$(jq -r '.bao_target // "pilot"' "$CUSTODY" 2>/dev/null || echo pilot)}"
case $BAO_TARGET in
  platform-eks) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-bravo-platform}" ;;
  platform-rke2) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}" ;;
  *) PLATFORM_CONTEXT="${BRAVO_PLATFORM_CONTEXT:-}" ;;
esac
if [[ $BAO_TARGET == platform-* ]]; then
  BAO_CA="$CUSTODY_DIR/$BAO_TARGET/ca.crt"
  BAO_ADDR=""   # set by bao_pf_start
else
  BAO_CA="$PILOT_DIR/bao/tls/ca.crt"
  BAO_ADDR="${BAO_ADDR:-https://127.0.0.1:8200}"
fi
BAO_PF_PID=""
STATE_DIR="${BRAVO_TARGET_GCP_STATE:-$HOME/.bravo/target-gcp}"   # link pids, status files (no secrets)
ORIG_KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

die() { printf '%s: %s\n' "$(basename "$0")" "$*" >&2; exit 1; }
log() { printf '%s %s: %s\n' "$(date -u +%H:%M:%SZ)" "$(basename "$0")" "$*"; }

# gke-gcloud-auth-plugin ships in the gcloud SDK bin directory, which is not always on PATH.
GCLOUD_BIN="$(dirname "$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$(command -v gcloud)")")"
export PATH="$GCLOUD_BIN:$PATH"
export USE_GKE_GCLOUD_AUTH_PLUGIN=True

tf() { terraform -chdir="$TG_ENV_DIR" "$@"; }
tf_out() { tf output -raw "$1"; }

load_outputs() {
  PROJECT="$(tf_out project_id)"
  REGION="$(tf_out region)"
  CLUSTER="$(tf_out cluster_name)"
  LOCATION="$(tf_out cluster_location)"
  # GKE kubeconfig first, then the operator's own (bravo-platform, bravo-source contexts for the links).
  # Every kubectl call names its --context, so the merged current-context does not matter.
  KUBECONFIG="$(tf_out kubeconfig_path):$ORIG_KUBECONFIG"; export KUBECONFIG
  IMAGE_REGISTRY="$(tf_out image_registry)"
  GATEWAY_ADDRESS_NAME="$(tf_out gateway_address_name)"
  GATEWAY_IP="$(tf_out gateway_address)"
  PROXY_ONLY_CIDR="$(tf_out proxy_only_cidr)"
  PD_KMS_KEY="$(tf_out pd_kms_key_id)"
  ATTACH_BUCKET="$(tf_out attachments_bucket)"
  DB_BACKUP_BUCKET="$(tf_out db_backup_bucket)"   # CNPG chat-db barman-cloud (db_backup.tf)
  CHAT_APP_SA="$(tf_out chat_app_service_account)"
  GATE_OTHER_SA="$(tf_out gate_other_service_account)"
  CLUSTER_NAME="$CLUSTER"
  PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-b-free-chat-gke.mangowhois.ai}"
  export PROJECT REGION CLUSTER LOCATION IMAGE_REGISTRY GATEWAY_ADDRESS_NAME GATEWAY_IP PROXY_ONLY_CIDR \
    PD_KMS_KEY ATTACH_BUCKET DB_BACKUP_BUCKET CLUSTER_NAME PUBLIC_HOSTNAME
  # shellcheck disable=SC1091
  set -a; . "$REPO_ROOT/deploy/landing-zone/versions.env"; . "$OVERLAY_DIR/versions.env"; set +a
}

k() { kubectl --context "${CLUSTER:?cluster name not loaded}" "$@"; }

# ---- platform OpenBao (same custody model as deploy/pilot/scripts/keys.sh) -------------------------------
tok_hdr() { [[ -z ${1-} ]] || printf 'X-Vault-Token: %s\n' "$1"; }

bao_pf_stop() { [[ -z $BAO_PF_PID ]] || { kill "$BAO_PF_PID" 2>/dev/null || true; wait "$BAO_PF_PID" 2>/dev/null || true; BAO_PF_PID=""; }; }
# Private port-forward to the platform EKS OpenBao on a free local port, for this process only.
bao_pf_start() {
  [[ $BAO_TARGET == platform-* && -z $BAO_PF_PID ]] || return 0
  [[ -f $BAO_CA ]] || die "no $BAO_CA (keys.sh --target $BAO_TARGET tls)"
  local port i code
  port="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
  kubectl --context "$PLATFORM_CONTEXT" -n openbao port-forward pod/openbao-0 "$port:8200" >/dev/null 2>&1 &
  BAO_PF_PID=$!
  trap bao_pf_stop EXIT
  BAO_ADDR="https://127.0.0.1:$port"
  for i in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$BAO_CA" --max-time 2 "$BAO_ADDR/v1/sys/seal-status" || true)"
    [[ $code == 200 ]] && return 0
    kill -0 "$BAO_PF_PID" 2>/dev/null || die "port-forward to $PLATFORM_CONTEXT openbao/openbao-0 exited"
    sleep 0.25
  done
  die "platform OpenBao not reachable through port-forward ($PLATFORM_CONTEXT openbao/openbao-0)"
}

# bao [-d|-p] METHOD PATH [TOKEN]; -d: JSON body on stdin, -p: KV v2 merge-patch body on stdin.
# Token goes to curl through a pipe, never argv.
bao() {
  bao_pf_start
  local body=0 ctype=application/json
  [[ $1 == -d ]] && { body=1; shift; }
  [[ $1 == -p ]] && { body=1; ctype=application/merge-patch+json; shift; }
  local method=$1 path=$2 token=${3-} out code
  if ((body)); then
    out="$(curl -sS --cacert "$BAO_CA" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -H "Content-Type: $ctype" --data-binary @- -w '\n%{http_code}' "$BAO_ADDR/v1/$path")" \
      || die "cannot reach OpenBao at $BAO_ADDR"
  else
    out="$(curl -sS --cacert "$BAO_CA" --max-time 30 -X "$method" -H @<(tok_hdr "$token") \
      -w '\n%{http_code}' "$BAO_ADDR/v1/$path" </dev/null)" || die "cannot reach OpenBao at $BAO_ADDR"
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  if [[ $code != 2* ]]; then
    printf '%s: OpenBao %s %s -> HTTP %s\n' "$(basename "$0")" "$method" "$path" "$code" >&2
    return 1
  fi
  printf '%s' "$out"
}

bao_admin_token() {
  [[ -f $CUSTODY ]] || die "no custody file $CUSTODY"
  jq -er .admin_token "$CUSTODY"
}

need_bao() {
  local st; st="$(bao GET sys/seal-status)" || die "platform OpenBao ($BAO_TARGET) unreachable"
  [[ $(jq -r .sealed <<<"$st") == false ]] || die "platform OpenBao is sealed: deploy/pilot/scripts/keys.sh unseal"
}

bao_exists() { bao GET "secret/metadata/envs/$BAO_ENV/$1" "$2" >/dev/null 2>&1; }
