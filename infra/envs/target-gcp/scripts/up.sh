#!/usr/bin/env bash
# One command: GKE target (infra) + landing zone (deploy/landing-zone/overlays/gke) + demo app.
# Idempotent: re-running converges. Secrets only from the platform OpenBao (D-26), never printed.
#
#   infra/envs/target-gcp/scripts/up.sh            everything
#   SKIP_TF=1 .../up.sh                            landing zone and app only (cluster exists)
#   PGB_INITIAL_TARGET=target-dst .../up.sh        PgBouncer starts on the local DB instead of the
#                                                  contract default target-src (source DB over the link)
#
# Needs: terraform 1.17, gcloud (account with access to the project), kubectl, helm, jq, envsubst, openssl,
# AWS credentials for the S3 state backend, the platform OpenBao unsealed (deploy/pilot/scripts/keys.sh).
source "$(dirname "$0")/lib.sh"

PGB_INITIAL_TARGET="${PGB_INITIAL_TARGET:-target-src}"
[[ $PGB_INITIAL_TARGET == target-src || $PGB_INITIAL_TARGET == target-dst ]] || die "PGB_INITIAL_TARGET: target-src|target-dst"
export PGB_INITIAL_TARGET
RENDER_DIR="$STATE_DIR/rendered"
CNPG_REPO=https://cloudnative-pg.github.io/charts
ESO_REPO=https://charts.external-secrets.io

for b in terraform gcloud kubectl helm jq envsubst openssl curl; do command -v "$b" >/dev/null || die "needs $b"; done
need_bao

# ---- 1. infrastructure ---------------------------------------------------------------------------------------
if [[ -z ${SKIP_TF-} ]]; then
  log "terraform init/apply (state: S3 backend, key envs/target-gcp.tfstate)"
  tf init -input=false -backend-config=backend.hcl >/dev/null
  tf apply -input=false -auto-approve
fi
load_outputs
mkdir -p "$STATE_DIR" "$RENDER_DIR"; chmod 700 "$STATE_DIR"
log "project=$PROJECT cluster=$CLUSTER location=$LOCATION"

# GKE adds kubernetes.io/arch=arm64:NoSchedule to Arm nodes. The whole cluster is Arm (C4A), so the taint
# would only force a toleration into every chart. Terraform google 8.9 has no field for it (gcloud only).
if [[ $(gcloud container node-pools describe core --cluster "$CLUSTER" --location "$LOCATION" --project "$PROJECT" \
      --format='value(nodeConfig.taints)' 2>/dev/null) == *arm64* ]] \
   || k get nodes -o json | jq -e '[.items[].spec.taints[]? | select(.key=="kubernetes.io/arch")] | length > 0' >/dev/null; then
  log "node pool core: architecture taint behavior -> none"
  gcloud container node-pools update core --cluster "$CLUSTER" --location "$LOCATION" --project "$PROJECT" \
    --node-architecture-taint-behavior=none --quiet >/dev/null
fi
k wait --for=condition=Ready nodes --all --timeout=600s >/dev/null

# ---- 2. images: the destination registry must hold the same digests --------------------------------------------
for spec in "$DEMO_IMAGE_NAME@$DEMO_DIGEST" "$PGB_IMAGE_NAME@$PGB_DIGEST"; do
  gcloud artifacts docker images describe "$IMAGE_REGISTRY/$spec" --project "$PROJECT" >/dev/null 2>&1 \
    || die "image $IMAGE_REGISTRY/$spec missing (push it first, see README 'Images')"
  log "image present: $IMAGE_REGISTRY/$spec"
done

# ---- 3. secrets in the platform OpenBao ------------------------------------------------------------------------
"$TG_SCRIPTS/secrets.sh" seed

# ---- 4. operators (same chart versions as every overlay) --------------------------------------------------------
log "helm: cloudnative-pg $CNPG_CHART_VERSION, external-secrets $ESO_CHART_VERSION"
helm --kube-context "$CLUSTER" upgrade --install cnpg cloudnative-pg --repo "$CNPG_REPO" --version "$CNPG_CHART_VERSION" \
  -n cnpg-system --create-namespace -f "$OVERLAY_DIR/values/cnpg-operator.yaml" --wait --timeout 10m >/dev/null
helm --kube-context "$CLUSTER" upgrade --install external-secrets external-secrets --repo "$ESO_REPO" --version "$ESO_CHART_VERSION" \
  -n external-secrets --create-namespace -f "$OVERLAY_DIR/values/eso.yaml" --wait --timeout 10m >/dev/null

# ---- 5. render the overlay -----------------------------------------------------------------------------------------
API_SERVER_CIDR="$(k get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o json \
  | jq -r '[.items[].endpoints[].addresses[]][0]')/32"
BAO_CA_B64="$(base64 <"$BAO_CA" | tr -d '\n')"   # CA certificate (public), not a secret
export API_SERVER_CIDR BAO_CA_B64
VARS='${PD_KMS_KEY} ${BAO_CA_B64} ${PG_IMAGE} ${IMAGE_REGISTRY} ${PGB_IMAGE_NAME} ${PGB_DIGEST} ${PGB_INITIAL_TARGET}
${GATEWAY_ADDRESS_NAME} ${GATEWAY_IP} ${CLOUDFLARED_IMAGE} ${OTEL_IMAGE} ${CLUSTER_NAME} ${REGION} ${API_SERVER_CIDR}
${DEMO_IMAGE_NAME} ${DEMO_DIGEST} ${PUBLIC_HOSTNAME} ${ATTACH_BUCKET} ${PROXY_ONLY_CIDR}'
rm -f "$RENDER_DIR"/*.yaml
for f in "$OVERLAY_DIR"/manifests/*.yaml "$OVERLAY_DIR"/values/chat.yaml; do
  envsubst "$VARS" <"$f" >"$RENDER_DIR/$(basename "$f")"
done
if grep -n '\${[A-Z_]*}' "$RENDER_DIR"/*.yaml; then die "unrendered variables above"; fi
"$TG_SCRIPTS/links.sh" render >"$RENDER_DIR/05-links.yaml"
log "rendered overlay -> $RENDER_DIR"

# ---- 6. apply: namespaces, storage, link relays, secret store --------------------------------------------------------
k apply -f "$RENDER_DIR/00-namespaces.yaml" -f "$RENDER_DIR/10-storageclasses.yaml" >/dev/null
k apply -f "$RENDER_DIR/05-links.yaml" >/dev/null
for d in external-secrets/openbao bravo-observability/platform-otel chat/src-db-link; do
  k -n "${d%/*}" rollout status "deploy/${d#*/}" --timeout=300s >/dev/null
done
"$TG_SCRIPTS/links.sh" start
"$TG_SCRIPTS/secrets.sh" eso
k apply -f "$RENDER_DIR/20-secretstore.yaml" >/dev/null
k wait --for=condition=Ready clustersecretstore/platform-bao --timeout=180s >/dev/null \
  || die "ClusterSecretStore platform-bao not Ready (link to platform OpenBao? see links.sh status)"
log "ClusterSecretStore platform-bao Ready (ESO <- platform OpenBao over the link)"
k apply -f "$RENDER_DIR/21-externalsecrets.yaml" >/dev/null
# Re-read OpenBao now (a re-run may have changed a value, e.g. app_writer copied from the source).
for ns in chat cf-edge; do k -n "$ns" annotate externalsecret --all force-sync="$(date +%s)" --overwrite >/dev/null; done
sleep 5
k wait --for=condition=Ready -n chat externalsecret --all --timeout=180s >/dev/null
k wait --for=condition=Ready -n cf-edge externalsecret --all --timeout=180s >/dev/null
log "ExternalSecrets synced"

# ---- 7. database: CNPG chat-db + schema (01 -> 02 -> 03_dst) ---------------------------------------------------
k apply -f "$RENDER_DIR/60-networkpolicies.yaml" >/dev/null
k apply -f "$RENDER_DIR/30-chat-db.yaml" >/dev/null
log "waiting for CNPG chat-db"
for _ in $(seq 1 90); do
  [[ $(k -n chat get cluster chat-db -o jsonpath='{.status.readyInstances}' 2>/dev/null) == 1 ]] && break
  sleep 10
done
k -n chat wait --for=condition=Ready cluster/chat-db --timeout=60s >/dev/null
# Schema and role passwords with the data lane's tool (deploy/cloud/db_roles.py, same as the other targets):
# SCRAM verifiers computed on the laptop, sent on stdin through kubectl exec (CNPG local peer auth).
bao_pf_start
BRAVO_BAO_LOCAL_PORT="${BAO_ADDR##*:}" python3 -I "$REPO_ROOT/deploy/cloud/db_roles.py" \
  --env "$BAO_ENV" --context "$CLUSTER" --schema dst --store-id gcp | tee "$STATE_DIR/db_roles.json"

# ---- 8. PgBouncer, OTel agent, Gateway, cf-edge -----------------------------------------------------------------
k apply -f "$RENDER_DIR/31-pgbouncer.yaml" -f "$RENDER_DIR/50-otel-agent.yaml" >/dev/null
# Roll PgBouncer and the app when their Secrets change (auth_file is read at start).
AUTH_RV="$(k -n chat get secret chat-pgbouncer-auth -o jsonpath='{.metadata.resourceVersion}')"
DB_RV="$(k -n chat get secret bravo-demo-db -o jsonpath='{.metadata.resourceVersion}')"
k -n chat patch deploy chat-pgbouncer --type merge \
  -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"bravo.io/auth-rv\":\"$AUTH_RV\"}}}}}" >/dev/null
k -n chat rollout status deploy/chat-pgbouncer --timeout=300s >/dev/null
k -n bravo-observability rollout status ds/otel-agent --timeout=300s >/dev/null
log "chat-pgbouncer (initial $PGB_INITIAL_TARGET) and otel-agent ready"

# ---- 9. demo app (same chart, same digest) ------------------------------------------------------------------------
helm --kube-context "$CLUSTER" upgrade --install chat "$REPO_ROOT/apps/demo/chart" -n chat \
  -f "$RENDER_DIR/chat.yaml" --set-string "podAnnotations.bravo\.io/db-rv=$DB_RV" --wait --timeout 10m >/dev/null
log "helm release chat deployed ($IMAGE_REGISTRY/$DEMO_IMAGE_NAME@$DEMO_DIGEST)"

k apply -f "$RENDER_DIR/40-gateway.yaml" -f "$RENDER_DIR/41-cf-edge.yaml" >/dev/null
k -n cf-edge rollout status deploy/cloudflared --timeout=300s >/dev/null
log "waiting for Gateway bravo-gateway to be programmed (Google creates the internal load balancer)"
k -n bravo-edge wait --for=condition=Programmed gateway/bravo-gateway --timeout=900s >/dev/null
log "Gateway programmed at $(k -n bravo-edge get gateway bravo-gateway -o jsonpath='{.status.addresses[0].value}')"
log "done. Verify: $TG_SCRIPTS/verify.sh   Public: https://$PUBLIC_HOSTNAME"
