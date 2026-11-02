#!/usr/bin/env bash
# One command teardown of the GKE target. Order matters:
#   1. stop the laptop link agents
#   2. delete what makes Google resources outside Terraform (Gateway -> load balancer and NEGs,
#      CNPG -> Hyperdisk volumes) and wait, so nothing is orphaned or blocks the VPC deletion
#   3. revoke the GCS HMAC keys and the ESO AppRole (OpenBao keeps the other target-gcp values)
#   4. terraform destroy (deletion_protection=false in terraform.tfvars)
# The Cloudflare tunnel bravo-b-free-chat-gke (infra/envs/edge) costs nothing and is kept.
source "$(dirname "$0")/lib.sh"

tf init -input=false -backend-config=backend.hcl >/dev/null
if tf output -raw cluster_name >/dev/null 2>&1; then
  load_outputs
  "$TG_SCRIPTS/links.sh" stop || true
  if k get ns >/dev/null 2>&1; then
    log "deleting Gateway, app, database (load balancer, NEGs and disks are released by GKE)"
    k -n bravo-edge delete gateway bravo-gateway --ignore-not-found --wait=true --timeout=600s || true
    k -n chat delete httproute chat --ignore-not-found || true
    helm --kube-context "$CLUSTER" -n chat uninstall chat --wait >/dev/null 2>&1 || true
    k -n chat delete cluster.postgresql.cnpg.io chat-db --ignore-not-found --wait=true --timeout=300s || true
    k -n chat delete pvc --all --wait=true --timeout=300s || true
    for _ in $(seq 1 30); do
      [[ $(k get pv -o json | jq '[.items[] | select(.spec.storageClassName | IN("bravo-db","bravo-general"))] | length') == 0 ]] && break
      sleep 10
    done
    # NEGs are deleted asynchronously after the Gateway is gone.
    for _ in $(seq 1 30); do
      [[ -z $(gcloud compute network-endpoint-groups list --project "$PROJECT" --format='value(name)' 2>/dev/null) ]] && break
      sleep 10
    done
  fi
  "$TG_SCRIPTS/secrets.sh" revoke || true
fi
log "terraform destroy"
tf destroy -input=false -auto-approve
rm -rf "$STATE_DIR/rendered"
log "done"
