#!/usr/bin/env bash
# Install the landing zone (D-23, docs/architecture/11-landing-zone.md) on one cluster.
#   install.sh <overlay> <kube-context> [step ...]
# Overlay: rke2 (on-prem, spec §7.2; the eks and gke overlays are not written yet). Steps run in the order below;
# naming steps runs only those. Every step is idempotent.
#
# Secrets (D-26): values come only from the platform OpenBao through deploy/pilot/scripts/keys.sh
# (env-generate / env-pipe) and reach kubectl on a pipe. Nothing is written to a file or argv.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
keys="$repo/deploy/pilot/scripts/keys.sh"
overlay=${1:?usage: install.sh <overlay> <kube-context> [step ...]}
ctx=${2:?usage: install.sh <overlay> <kube-context> [step ...]}
shift 2
[[ -d $here/overlays/$overlay ]] || { echo "install.sh: no overlay $overlay" >&2; exit 1; }
ov="$here/overlays/$overlay"
# shellcheck source=versions.env
set -a; . "$here/versions.env"; set +a

ENV_ID=${BRAVO_ENV_ID:-target-onprem}
LIMA_SERVER=${BRAVO_LIMA_SERVER:-bravo-onprem-server}
LIMA_NODES=${BRAVO_LIMA_NODES:-"bravo-onprem-server bravo-onprem-agent"}
BLOCK_BACKING=${BRAVO_BLOCK_BACKING:-rbd}   # rbd (rbd-nbd mounter); local-path = labelled stopgap
OSD_DISK_BYTES=${BRAVO_OSD_DISK_BYTES:-32212254720}   # 30 GiB data disk (infra/envs/target-onprem)

k() { kubectl --context "$ctx" "$@"; }
h() { helm --kube-context "$ctx" "$@"; }
log() { printf '\n== landing-zone[%s/%s] %s\n' "$overlay" "$ctx" "$*"; }
render() { envsubst '${PG_IMAGE} ${CLOUDFLARED_IMAGE} ${PGBOUNCER_IMAGE} ${IMAGE_PULL_POLICY} ${CEPH_IMAGE} ${OSD_NODES}'; }

export PGBOUNCER_IMAGE="$PGBOUNCER_IMAGE_REPO@$PGBOUNCER_IMAGE_DIGEST"
# containerd CLI on the nodes (RKE2 bundles ctr; images are imported, never pulled).
CTR="sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock -n k8s.io"
export IMAGE_PULL_POLICY=Never

helm_repos() {
  helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
  helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
  helm repo add rook-release https://charts.rook.io/release >/dev/null 2>&1 || true
  helm repo add kyverno https://kyverno.github.io/kyverno >/dev/null 2>&1 || true
  helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
  helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
  helm repo add ceph-csi-operator https://ceph.github.io/ceph-csi-operator >/dev/null 2>&1 || true
  helm repo update >/dev/null
}

step_cilium() {
  log "Gateway API $GATEWAY_API_VERSION CRDs, Cilium $CILIUM_CHART_VERSION (VXLAN, kube-proxy replacement, Hubble; RKE2 cni: none)"
  k apply --server-side --force-conflicts \
    -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API_VERSION/standard-install.yaml" >/dev/null
  h upgrade --install cilium cilium/cilium --version "$CILIUM_CHART_VERSION" -n kube-system \
    -f "$ov/cilium-values.yaml" --wait --timeout 10m >/dev/null
  k -n kube-system rollout status ds/cilium --timeout=5m
  k -n kube-system rollout status deploy/cilium-operator --timeout=5m
  k -n kube-system rollout status deploy/hubble-relay --timeout=5m
  k wait --for=condition=Ready node --all --timeout=5m
  k get gatewayclass cilium
  k apply -f "$ov/lb-pool.yaml"
}

step_base() {
  log "namespaces, StorageClasses, PgBouncer, cloudflared (kustomize overlay $overlay)"
  k kustomize "$ov" | render | k apply -f -
}

step_rbd() {
  log "Ceph CSI drivers chart $CEPH_CSI_DRIVERS_CHART_VERSION (RBD) and RBD pool bravo-rbd"
  h upgrade --install ceph-csi-drivers ceph-csi-operator/ceph-csi-drivers --version "$CEPH_CSI_DRIVERS_CHART_VERSION" \
    -n rook-ceph -f "$ov/rook/csi-drivers-values.yaml" --wait --timeout 5m >/dev/null
  k apply -f "$ov/rook/rbd-pool.yaml"
  k -n rook-ceph wait --for=jsonpath='{.status.phase}'=Ready cephblockpool/bravo-rbd --timeout=5m
}

# Block StorageClasses bravo-db / bravo-general. BLOCK_BACKING=rbd (Rook RBD, rbd-nbd mounter) is the
# default; local-path is the labelled stopgap (see the overlay files).
step_storage() {
  log "StorageClasses bravo-db, bravo-general on $BLOCK_BACKING"
  local want cur sc
  case $BLOCK_BACKING in
    rbd) want=rook-ceph.rbd.csi.ceph.com ;;
    local-path) want=rancher.io/local-path
      k apply -f "$ov/local-path.yaml" >/dev/null
      k -n local-path-storage rollout status deploy/local-path-provisioner --timeout=3m ;;
    *) echo "BRAVO_BLOCK_BACKING must be rbd or local-path" >&2; return 1 ;;
  esac
  for sc in bravo-db bravo-general; do
    cur="$(k get sc "$sc" -o jsonpath='{.provisioner}' 2>/dev/null || true)"
    [[ -z $cur || $cur == "$want" ]] || { echo "recreating StorageClass $sc ($cur -> $want)"; k delete sc "$sc" >/dev/null; }
  done
  k apply -f "$ov/storageclasses-$BLOCK_BACKING.yaml"
  k get sc bravo-db bravo-general -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner,BACKING:.metadata.annotations.bravo\.dev/backing
}

step_images() {
  log "import platform images into containerd on every node (digest kept)"
  local local_ref repo dig n
  while read -r -u 3 local_ref repo dig; do
    [[ $(docker image inspect "$local_ref" --format '{{.Id}}') == "$dig" ]] \
      || { echo "image $local_ref is not $dig on this laptop" >&2; return 1; }
    docker tag "$local_ref" "$repo:import"
    for n in $LIMA_NODES; do
      if limactl shell "$n" -- $CTR images ls -q </dev/null | grep -qx "$repo@$dig"; then
        echo "$n: $repo@$dig present"; continue
      fi
      docker save "$repo:import" | limactl shell "$n" -- sh -c 'cat > /tmp/bravo-image.tar'
      limactl shell "$n" -- $CTR images import --platform linux/arm64 /tmp/bravo-image.tar </dev/null >/dev/null
      limactl shell "$n" -- $CTR images tag --force "$repo:import" "$repo@$dig" </dev/null >/dev/null
      limactl shell "$n" -- sudo rm -f /tmp/bravo-image.tar </dev/null
      limactl shell "$n" -- $CTR images ls -q </dev/null | grep -qx "$repo@$dig"
      echo "$n: imported $repo@$dig"
    done
    docker rmi "$repo:import" >/dev/null
  done 3<<LIST
$DEMO_IMAGE_LOCAL $DEMO_IMAGE_REPO $DEMO_IMAGE_DIGEST
$PGBOUNCER_IMAGE_LOCAL $PGBOUNCER_IMAGE_REPO $PGBOUNCER_IMAGE_DIGEST
LIST
}

step_cnpg() {
  log "CloudNativePG chart $CNPG_CHART_VERSION, Cluster chat-db"
  h upgrade --install cnpg cnpg/cloudnative-pg --version "$CNPG_CHART_VERSION" -n cnpg-system \
    --set config.clusterWide=true --wait --timeout 5m >/dev/null
  k -n cnpg-system rollout status deploy/cnpg-cloudnative-pg --timeout=5m
  render <"$here/base/chat-db.yaml" | k apply -f -
  k -n chat wait --for=condition=Ready cluster/chat-db --timeout=10m
}

# secret_from_bao <ns> <secret> <key>=<bao name>/<bao field> ...
# Each value goes keys.sh env-pipe -> base64 -> jq -> kubectl apply, all on pipes (bash 3.2 safe).
secret_from_bao() {
  local ns=$1 name=$2; shift 2
  local spec key path
  for spec in "$@"; do
    key=${spec%%=*}; path=${spec#*=}
    printf '%s\t' "$key"
    "$keys" env-pipe "$ENV_ID" "${path%%/*}" "${path#*/}" -- base64 | tr -d '\n'
    printf '\n'
  done | jq -Rn --arg ns "$ns" --arg name "$name" \
    '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: $name, namespace: $ns},
      data: ([inputs | split("\t") | {(.[0]): .[1]}] | add)}' | k apply -f - >/dev/null
  echo "secret $ns/$name <- OpenBao secret/envs/$ENV_ID: $(printf '%s ' "$@" | sed 's/[a-z_-]*=//g; s/ $//') (values not shown)"
}

step_secrets() {
  log "Secrets from the platform OpenBao (secret/envs/$ENV_ID/*)"
  "$keys" env-generate "$ENV_ID" chat-admin token
  # PgBouncer-local logins are environment-local. app_writer is NOT generated: it must equal the
  # source DB's value while target-src.ini points at the source; copy it inside OpenBao with
  # keys.sh env-copy source-aws chat-db app_writer target-onprem.
  "$keys" env-generate "$ENV_ID" chat-db pgb_admin
  "$keys" env-generate "$ENV_ID" chat-db pgb_stats
  secret_from_bao chat chat-admin token=chat-admin/token
  secret_from_bao chat chat-pgbouncer-auth app_writer=chat-db/app_writer pgb_admin=chat-db/pgb_admin pgb_stats=chat-db/pgb_stats
  # The DSN carries no password; the password is a separate key (chart db.passwordSecret).
  local dsn="host=chat-pgbouncer.chat.svc.cluster.local port=6432 dbname=chat user=app_writer"
  "$keys" env-pipe "$ENV_ID" chat-db app_writer -- \
    kubectl --context "$ctx" -n chat create secret generic bravo-demo-db --from-literal=dsn="$dsn" \
      --from-file=password=/dev/stdin --dry-run=client -o yaml | k apply -f - >/dev/null
  echo "secret chat/bravo-demo-db <- dsn (no password) + OpenBao chat-db/app_writer"
  "$keys" env-pipe "$ENV_ID" cf-tunnel token -- \
    kubectl --context "$ctx" -n cf-edge create secret generic cf-tunnel --from-file=token=/dev/stdin \
      --dry-run=client -o yaml | k apply -f - >/dev/null
  echo "secret cf-edge/cf-tunnel <- OpenBao cf-tunnel/token"
  k -n chat rollout restart deploy/chat-pgbouncer >/dev/null 2>&1 || true
  k -n cf-edge rollout restart deploy/cloudflared >/dev/null 2>&1 || true
}

step_app() {
  log "demo app: Helm release chat (apps/demo/chart; k3s-values + render-values.sh onprem + overlay)"
  k -n chat get secret bravo-demo-attachments >/dev/null 2>&1 \
    || { echo "RGW bucket secret chat/bravo-demo-attachments missing: run steps rook rgw_tls rgw first" >&2; return 1; }
  # App S3 credential = the OBC owner's keys (generated by RGW in-cluster), renamed on a pipe.
  k -n chat get secret bravo-demo-attachments -o json \
    | jq '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: "bravo-demo-s3", namespace: "chat"},
           data: {"access-key-id": .data.AWS_ACCESS_KEY_ID, "secret-access-key": .data.AWS_SECRET_ACCESS_KEY}}' \
    | k apply -f - >/dev/null
  echo "secret chat/bravo-demo-s3 <- chat/bravo-demo-attachments (OBC keys, values not shown)"
  local gen="$repo/apps/demo/chart/values-onprem.generated.yaml"
  "$repo/apps/demo/scripts/render-values.sh" onprem --image-repository "$DEMO_IMAGE_REPO" \
    --image-digest "$DEMO_IMAGE_DIGEST" --public-url https://b-free-chat-dst.mangowhois.ai \
    --s3-endpoint https://rook-ceph-rgw-bravo.rook-ceph.svc --s3-bucket bravo-demo-attachments --out "$gen"
  k apply -f "$ov/chat-app-ingress.yaml"
  # No --wait: /readyz needs a real DB read, which on the destination goes PgBouncer -> link.
  h upgrade --install chat "$repo/apps/demo/chart" -n chat \
    -f "$repo/apps/demo/chart/ci/k3s-values.yaml" -f "$gen" -f "$ov/chat-values.yaml" >/dev/null
  k -n chat rollout status deploy/chat-pgbouncer --timeout=3m
  k -n cf-edge rollout status deploy/cloudflared --timeout=3m
  k -n chat rollout status deploy/chat-bravo-demo --timeout=90s \
    || echo "chat-bravo-demo: new pods not Ready (readiness needs a DB read through PgBouncer target-src = link)"
}

# The OSD disk of each node: the raw disk of the expected size with no partition and no filesystem
# (or Ceph's own bluestore label on a re-run). Not by device name: with vz the cloud-init ISO is
# also a block device and the order is not fixed.
osd_disk() {
  limactl shell "$1" -- lsblk -b -dn -o NAME,SIZE,TYPE,RO,FSTYPE </dev/null | awk -v sz="$OSD_DISK_BYTES" '
    $3 == "disk" && $4 == "0" && $2 == sz && ($5 == "" || $5 == "ceph_bluestore") {print $1}' | while read -r d; do
      [[ $(limactl shell "$1" -- lsblk -n -o NAME "/dev/$d" </dev/null | wc -l | tr -d ' ') == 1 ]] && echo "$d"
    done | head -n1
}

step_rook() {
  log "Rook $ROOK_CHART_VERSION operator, CephCluster (mon 1, mgr 1, OSD per node on its raw data disk)"
  k apply -f "$ov/rook/namespace.yaml"
  h upgrade --install rook-ceph rook-release/rook-ceph --version "$ROOK_CHART_VERSION" -n rook-ceph \
    -f "$ov/rook/operator-values.yaml" --wait --timeout 10m >/dev/null
  k -n rook-ceph rollout status deploy/rook-ceph-operator --timeout=5m
  local n d nodes=()
  for n in $LIMA_NODES; do
    d="$(osd_disk "$n")"
    [[ -n $d ]] || { echo "no empty $OSD_DISK_BYTES-byte disk on $n" >&2; return 1; }
    echo "$n: OSD disk /dev/$d (size $OSD_DISK_BYTES, no partition, no filesystem)"
    nodes+=("{\"name\":\"$n\",\"devices\":[{\"name\":\"$d\"}]}")
  done
  OSD_NODES="[$(IFS=,; echo "${nodes[*]}")]"; export OSD_NODES
  render <"$ov/rook/cluster.yaml" | k apply -f -
  local i
  for i in $(seq 1 120); do
    [[ $(k -n rook-ceph get pods -l app=rook-ceph-osd --field-selector=status.phase=Running -o name 2>/dev/null | wc -l | tr -d ' ') -ge 2 ]] && break
    sleep 10
  done
  k -n rook-ceph get cephcluster rook-ceph -o jsonpath='{.status.phase} {.status.ceph.health}{"\n"}'
  k -n rook-ceph get pods -l app=rook-ceph-osd -o wide
}

# TLS for RGW: CA and server key are generated in memory and go to the cluster on a pipe; the CA
# key is dropped. Created once (re-runs keep the cert so clients keep trusting the same CA).
step_rgw_tls() {
  log "RGW TLS (Secret rook-ceph/rgw-bravo-tls, CA ConfigMap bravo-rgw-ca in chat and rook-ceph)"
  if k -n rook-ceph get secret rgw-bravo-tls >/dev/null 2>&1; then echo "rgw-bravo-tls exists; kept"; else
    local ca_key ca_crt srv_key srv_crt host=rook-ceph-rgw-bravo.rook-ceph.svc
    ca_key="$(openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 2>/dev/null)"
    ca_crt="$(openssl req -x509 -new -key <(printf '%s\n' "$ca_key") -days 90 -subj "/CN=bravo-onprem RGW CA" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null)"
    srv_key="$(openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 2>/dev/null)"
    srv_crt="$(openssl req -new -key <(printf '%s\n' "$srv_key") -subj "/CN=$host" 2>/dev/null \
      | openssl x509 -req -CA <(printf '%s\n' "$ca_crt") -CAkey <(printf '%s\n' "$ca_key") -days 60 \
        -set_serial "0x$(openssl rand -hex 16)" -extfile <(printf '%s\n' "basicConstraints=critical,CA:FALSE" \
        "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth" \
        "subjectAltName=DNS:$host,DNS:$host.cluster.local,DNS:rook-ceph-rgw-bravo,DNS:localhost,IP:127.0.0.1") 2>/dev/null)"
    unset ca_key
    jq -n --arg c "$srv_crt"$'\n'"$ca_crt" --arg k "$srv_key" \
      '{apiVersion: "v1", kind: "Secret", type: "kubernetes.io/tls",
        metadata: {name: "rgw-bravo-tls", namespace: "rook-ceph"},
        data: {"tls.crt": ($c + "\n" | @base64), "tls.key": ($k + "\n" | @base64)}}' | k apply -f - >/dev/null
    unset srv_key
    for n in chat rook-ceph; do
      k -n "$n" create configmap bravo-rgw-ca --from-literal=ca.crt="$ca_crt" --dry-run=client -o yaml | k apply -f - >/dev/null
    done
    echo "rgw-bravo-tls created (EC P-256, 60 days; CA 90 days, CA key discarded)"
  fi
}

step_rgw() {
  log "RGW object store bravo, bucket bravo-demo-attachments (OBC), OBJ-05 user"
  # objectstore.yaml has security.kms (SSE-KMS, key in the platform OpenBao): Rook needs these Secrets first.
  if grep -q 'tokenSecretName: rgw-kms-token' "$ov/rook/objectstore.yaml" \
    && ! k -n rook-ceph get secret rgw-kms-token rgw-kms-ca >/dev/null 2>&1; then
    echo "RGW SSE-KMS Secrets missing: run deploy/platform/scripts/onprem-tokens.sh rgw-kms first" >&2; return 1
  fi
  render <"$ov/rook/objectstore.yaml" | k apply -f -
  k -n rook-ceph wait --for=jsonpath='{.status.phase}'=Ready cephobjectstore/bravo --timeout=15m
  k -n chat wait --for=jsonpath='{.status.phase}'=Bound objectbucketclaim/bravo-demo-attachments --timeout=5m
  k -n rook-ceph wait --for=jsonpath='{.status.phase}'=Ready cephobjectstoreuser/bravo-gate-other --timeout=5m
  k -n rook-ceph get svc rook-ceph-rgw-bravo
  "$here/scripts/rgw-bucket.sh" "$ctx"
  # T8 (D-26): the Executor's S3 -> RGW attachment copy reads the bucket credential from the platform
  # OpenBao. OBC keys and the CA are regenerated with the cluster, so they are re-piped on every run.
  { k -n chat get secret bravo-demo-attachments -o json; k -n chat get configmap bravo-rgw-ca -o json
    k -n chat get configmap bravo-demo-attachments -o json; } \
    | jq -sc '{access_key_id: (.[0].data.AWS_ACCESS_KEY_ID | @base64d), secret_access_key: (.[0].data.AWS_SECRET_ACCESS_KEY | @base64d),
               endpoint: "https://rook-ceph-rgw-bravo.rook-ceph.svc", bucket: .[2].data.BUCKET_NAME, ca_pem: .[1].data["ca.crt"]}' \
    | "$keys" env-put-json "$ENV_ID" object-store
  # B7: rehearsal test bucket bravo-rehearsal-verify and its owner's key -> OpenBao secret/envs/<env>/rehearsal-s3.
  "$here/scripts/rgw-rehearsal-bucket.sh" "$ctx" "$ENV_ID"
}

# DR-01: RGW user + bucket for the chat-db backup (keys from the platform OpenBao, D-26). Cluster chat-db
# (base/chat-db.yaml, step cnpg) archives WAL there; until this step has run, archiving retries.
step_db_backup() {
  log "RGW user chat-db-backup, bucket bravo-chat-db-backup, Secrets chat/chat-db-backup-{s3,ca}"
  "$here/scripts/rgw-backup-bucket.sh" "$ctx" "$ENV_ID"
}

step_kyverno() {
  log "Kyverno chart $KYVERNO_CHART_VERSION + GeneratingPolicy bravo-default-deny"
  h upgrade --install kyverno kyverno/kyverno --version "$KYVERNO_CHART_VERSION" -n kyverno \
    -f "$here/base/kyverno-values.yaml" --wait --timeout 8m >/dev/null
  k apply -f "$here/base/default-deny.yaml"
  k wait --for=jsonpath='{.status.conditionStatus.ready}'=true generatingpolicy/bravo-default-deny --timeout=2m
}

step_eso() {
  log "External Secrets chart $ESO_CHART_VERSION + ClusterSecretStore (pending link)"
  h upgrade --install external-secrets external-secrets/external-secrets --version "$ESO_CHART_VERSION" \
    -n external-secrets --set installCRDs=true --wait --timeout 8m >/dev/null
  k apply -f "$here/base/eso-store.yaml"
}

step_otel() {
  log "OTel Collector agent (chart $OTEL_CHART_VERSION) in bravo-observability"
  h upgrade --install otel-agent open-telemetry/opentelemetry-collector --version "$OTEL_CHART_VERSION" \
    -n bravo-observability -f "$here/base/otel-agent-values.yaml" \
    --set image.repository="$OTEL_IMAGE_REPO" --set image.digest="$OTEL_IMAGE_DIGEST" --wait --timeout 5m >/dev/null
  k -n bravo-observability rollout status deploy/otel-agent --timeout=3m
}

step_gateway() {
  log "Gateway bravo-gateway (Cilium) + HTTPRoute chat"
  k apply -f "$here/base/gateway.yaml"
  k -n bravo-edge wait --for=condition=Programmed gateway/bravo-gateway --timeout=3m
  k -n bravo-edge get svc cilium-gateway-bravo-gateway
}

all_steps=(cilium base images rook rbd storage cnpg secrets kyverno eso otel rgw_tls rgw db_backup app gateway)
steps=("$@"); ((${#steps[@]})) || steps=("${all_steps[@]}")
helm_repos
for s in "${steps[@]}"; do "step_$s"; done
log "done: ${steps[*]}"
