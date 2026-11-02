#!/usr/bin/env bash
# Verification of the GKE target: infrastructure, landing zone, app path, controls (04-governance.md 1절)
# and the object-store behaviour probes OBJ-01..06 (platform/gate probes/objstore.py) against GCS.
# Every line is a real observation: PASS, FAIL or UNKNOWN with the reason. Nothing is assumed.
# Evidence: infra/envs/target-gcp/runs/verify-<UTC>/ (results.jsonl, raw command outputs; no secret values).
source "$(dirname "$0")/lib.sh"
load_outputs

RUN="$TG_ENV_DIR/runs/verify-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN"
RESULTS="$RUN/results.jsonl"
need_bao   # one private port-forward to the platform OpenBao for the whole run
: >"$RESULTS"
declare -i NPASS=0 NFAIL=0 NUNK=0

rec() {   # rec ID VERDICT DETAIL
  printf '%-8s %-7s %s\n' "$1" "$2" "$3"
  jq -nc --arg id "$1" --arg v "$2" --arg d "$3" --arg at "$(date -u +%FT%TZ)" '{id: $id, verdict: $v, detail: $d, at: $at}' >>"$RESULTS"
  case $2 in PASS) NPASS+=1 ;; FAIL) NFAIL+=1 ;; *) NUNK+=1 ;; esac
}
check() {  # check ID "detail" -- command (exit 0 = PASS)
  local id=$1 d=$2; shift 3
  if "$@" >>"$RUN/$id.out" 2>&1; then rec "$id" PASS "$d"; else rec "$id" FAIL "$d"; fi
}

echo "== target-gcp verification $(date -u +%FT%TZ) project=$PROJECT cluster=$CLUSTER"

# ------------------------------------------------------------------------------------------------ infrastructure
gcloud container clusters describe "$CLUSTER" --location "$LOCATION" --project "$PROJECT" --format=json >"$RUN/cluster.json"
gcloud container node-pools describe core --cluster "$CLUSTER" --location "$LOCATION" --project "$PROJECT" --format=json >"$RUN/nodepool.json"
C="$RUN/cluster.json"; NP="$RUN/nodepool.json"
jcheck() { jq -e "$2" "$1" >/dev/null; }

check INF-01 "GKE Standard (not Autopilot)" -- jcheck "$C" '(.autopilot.enabled // false) == false'
check INF-02 "Dataplane V2: networkConfig.datapathProvider=$(jq -r .networkConfig.datapathProvider "$C")" -- jcheck "$C" '.networkConfig.datapathProvider == "ADVANCED_DATAPATH"'
check INF-03 "Kubernetes minor 1.36: control plane $(jq -r .currentMasterVersion "$C"), nodes $(jq -r .version "$NP")" -- jcheck "$C" '(.currentMasterVersion | startswith("1.36.")) and (.currentNodeVersion | startswith("1.36."))'
check INF-04 "Workload Identity: pool $(jq -r .workloadIdentityConfig.workloadPool "$C"), node metadata $(jq -r .config.workloadMetadataConfig.mode "$NP")" -- \
  bash -c "jq -e '.workloadIdentityConfig.workloadPool | endswith(\".svc.id.goog\")' '$C' && jq -e '.config.workloadMetadataConfig.mode == \"GKE_METADATA\"' '$NP'"
check INF-05 "private nodes, private endpoint; DNS endpoint (IAM) for operators" -- \
  jcheck "$C" '.privateClusterConfig.enablePrivateNodes and .privateClusterConfig.enablePrivateEndpoint and .controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic'
# Only this cluster's node VMs (other lanes may run VMs in the same project).
gcloud compute instances list --project "$PROJECT" --filter="labels.goog-k8s-cluster-name=$CLUSTER" --format=json >"$RUN/instances.json"
gcloud compute instances list --project "$PROJECT" --format='value(name,labels.component)' >"$RUN/instances-all.txt"
check INF-06 "no external IP on the cluster's node VMs ($(jq length "$RUN/instances.json") node(s))" -- \
  jcheck "$RUN/instances.json" '[.[].networkInterfaces[].accessConfigs[]?] | length == 0'
gcloud compute routers nats list --router "$CLUSTER-router" --region "$REGION" --project "$PROJECT" --format=json >"$RUN/nat.json"
check INF-07 "Cloud NAT for egress: $(jq -r '[.[].name] | join(",")' "$RUN/nat.json")" -- jcheck "$RUN/nat.json" 'length >= 1'
check INF-08 "Secret encryption at rest with CMEK (application-layer): $(jq -r .databaseEncryption.state "$C")" -- \
  jcheck "$C" '.databaseEncryption.state == "ENCRYPTED" and (.databaseEncryption.keyName | test("cryptoKeys/gke-secrets"))'
check INF-09 "node arch arm64 (C4A $(jq -r .config.machineType "$NP")), boot disk $(jq -r .config.diskType "$NP") + CMEK" -- \
  bash -c "k() { kubectl --context '$CLUSTER' \"\$@\"; }; k get nodes -o json | jq -e '[.items[].metadata.labels[\"kubernetes.io/arch\"]] | all(. == \"arm64\")' && jq -e '.config.bootDiskKmsKey | length > 0' '$NP'"
check INF-10 "Gateway API enabled: $(jq -r .networkConfig.gatewayApiConfig.channel "$C")" -- jcheck "$C" '.networkConfig.gatewayApiConfig.channel == "CHANNEL_STANDARD"'
check INF-11 "label component=target on cluster and node VMs" -- \
  bash -c "jq -e '.resourceLabels.component == \"target\"' '$C' && jq -e 'all(.[]; .labels.component == \"target\")' '$RUN/instances.json'"
gcloud storage buckets describe "gs://$ATTACH_BUCKET" --project "$PROJECT" --format=json >"$RUN/bucket.json"
check INF-12 "bucket $ATTACH_BUCKET: public access prevention enforced, uniform access, CMEK, label component=target" -- \
  jcheck "$RUN/bucket.json" '.public_access_prevention == "enforced" and .uniform_bucket_level_access == true and (.default_kms_key | test("cryptoKeys/attachments")) and .labels.component == "target"'
k get nodes -o wide >"$RUN/nodes.txt" 2>&1

# ------------------------------------------------------------------------------------------------ landing zone
check LZ-01 "namespaces chat, bravo-edge, cf-edge, bravo-observability enforce Pod Security restricted" -- \
  bash -c "kubectl --context '$CLUSTER' get ns chat bravo-edge cf-edge bravo-observability -o json | jq -e '[.items[].metadata.labels[\"pod-security.kubernetes.io/enforce\"]] | (length == 4 and all(. == \"restricted\"))'"
check LZ-02 "StorageClass bravo-db, bravo-general (pd.csi, hyperdisk-balanced, CMEK); chat-db PVC Bound" -- \
  bash -c "kubectl --context '$CLUSTER' get sc bravo-db bravo-general -o json | jq -e '[.items[] | select(.provisioner == \"pd.csi.storage.gke.io\" and (.parameters[\"disk-encryption-kms-key\"] | length > 0))] | length == 2' && kubectl --context '$CLUSTER' -n chat get pvc -l cnpg.io/cluster=chat-db -o json | jq -e '[.items[] | select(.status.phase == \"Bound\" and .spec.storageClassName == \"bravo-db\")] | length >= 1'"
check LZ-03 "CNPG operator ready (cnpg-system)" -- k -n cnpg-system rollout status deploy/cnpg-cloudnative-pg --timeout=30s
k -n chat get cluster chat-db -o json >"$RUN/chat-db.json" 2>>"$RUN/kubectl.err" || true
check LZ-04 "CNPG Cluster chat-db: $(jq -r '.status.phase // "absent"' "$RUN/chat-db.json"), ready $(jq -r '.status.readyInstances // 0' "$RUN/chat-db.json")/$(jq -r '.spec.instances // 0' "$RUN/chat-db.json")" -- \
  jcheck "$RUN/chat-db.json" '.status.phase == "Cluster in healthy state" and .status.readyInstances == .spec.instances'

PRIMARY="$(jq -r '.status.currentPrimary // ""' "$RUN/chat-db.json")"
pgq() { k -n chat exec -i "$PRIMARY" -c postgres -- psql -X -q -tA -U postgres -d chat -c "$1" 2>>"$RUN/psql.err"; }
if [[ -n $PRIMARY ]]; then
  pgq "select json_build_object(
    'server_version', current_setting('server_version'),
    'wal_level', current_setting('wal_level'),
    'max_replication_slots', current_setting('max_replication_slots')::int,
    'max_slot_wal_keep_size', current_setting('max_slot_wal_keep_size'),
    'publications', (select coalesce(json_agg(pubname), '[]') from pg_publication),
    'slots', (select coalesce(json_agg(slot_name), '[]') from pg_replication_slots),
    'roles', (select json_object_agg(rolname, rolcanlogin) from pg_roles where rolname in ('app_writer','replicator','executor_ctl','executor_observe','chat_owner')),
    'active_store', (select active_store from attachment_store where id = 1))" >"$RUN/db-facts.json" || true
fi
D="$RUN/db-facts.json"
check LZ-05 "PostgreSQL 16, wal_level=logical, max_replication_slots>=10, max_slot_wal_keep_size set: $(jq -c '{server_version, wal_level, max_replication_slots, max_slot_wal_keep_size}' "$D" 2>/dev/null)" -- \
  jcheck "$D" '(.server_version | startswith("16.")) and .wal_level == "logical" and .max_replication_slots >= 10 and .max_slot_wal_keep_size != "-1"'
check LZ-06 "chat schema (dst side): publication bravo_rev, slot bravo_rev_sub, 4 roles, app_writer NOLOGIN, active_store=gcp: $(jq -c '{publications, slots, roles, active_store}' "$D" 2>/dev/null)" -- \
  jcheck "$D" '(.publications | index("bravo_rev")) and (.slots | index("bravo_rev_sub")) and (.roles | keys | length == 5) and .roles.app_writer == false and .roles.replicator and .roles.executor_ctl and .active_store == "gcp"'

k -n external-secrets get clustersecretstore bravo-platform-openbao -o json >"$RUN/secretstore.json" 2>>"$RUN/kubectl.err" || true
check LZ-07 "ESO ClusterSecretStore bravo-platform-openbao Ready (platform OpenBao over the link): $(jq -r '[.status.conditions[]? | select(.type=="Ready") | .message] | first // "no status"' "$RUN/secretstore.json")" -- \
  jcheck "$RUN/secretstore.json" '[.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length == 1'
k get externalsecrets -A -o json >"$RUN/externalsecrets.json"
check LZ-08 "ExternalSecrets synced: $(jq -r '[.items[] | "\(.metadata.namespace)/\(.metadata.name)=\([.status.conditions[]? | select(.type=="Ready") | .status] | first)"] | join(" ")' "$RUN/externalsecrets.json")" -- \
  jcheck "$RUN/externalsecrets.json" '(.items | length) >= 6 and all(.items[]; [.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length == 1)'
check LZ-09 "no in-cluster vault: no OpenBao/Vault workload (D-26)" -- \
  bash -c "! kubectl --context '$CLUSTER' get deploy,sts -A -o json | jq -e '[.items[] | .spec.template.spec.containers[].image | select(test(\"openbao|hashicorp/vault\"))] | length > 0'"

PGB_POD="$(k -n chat get pod -l app.kubernetes.io/name=chat-pgbouncer -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
ACTIVE="$(k -n chat exec "$PGB_POD" -c pgbouncer -- cat /run/pgbouncer/active.ini 2>/dev/null | grep -o 'host=[^ ]*' || true)"
check LZ-10 "PgBouncer chat-pgbouncer ready, image digest $PGB_DIGEST, targets dir has target-src.ini + target-dst.ini, active $ACTIVE" -- \
  bash -c "kubectl --context '$CLUSTER' -n chat rollout status deploy/chat-pgbouncer --timeout=30s && kubectl --context '$CLUSTER' -n chat exec '$PGB_POD' -c pgbouncer -- sh -c 'ls /etc/pgbouncer/targets/target-src.ini /etc/pgbouncer/targets/target-dst.ini && tail -1 /etc/pgbouncer/conf/pgbouncer.ini | grep -q \"%include /run/pgbouncer/active.ini\"' && kubectl --context '$CLUSTER' -n chat get deploy chat-pgbouncer -o jsonpath='{.spec.template.spec.containers[0].image}' | grep -q '$PGB_DIGEST'"

# PgBouncer -> local chat-db path (target-dst) through the real pooler: SHOW SERVERS needs a server, so use
# the stats console over a port-forward. Password from OpenBao into this process's environment only.
T="$(bao_admin_token)"
PGB_STATS="$(bao GET "secret/data/envs/$BAO_ENV/chat-db" "$T" | jq -r .data.data.pgb_stats)"
LP="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
k -n chat port-forward svc/chat-pgbouncer "$LP:6432" >"$RUN/pf-pgbouncer.log" 2>&1 &
PF=$!; sleep 3
if PGPASSWORD="$PGB_STATS" psql -X -tA "host=127.0.0.1 port=$LP dbname=pgbouncer user=pgb_stats connect_timeout=5" \
     -c "SHOW DATABASES" >"$RUN/pgbouncer-databases.txt" 2>&1; then
  rec LZ-11 PASS "PgBouncer stats console reachable (auth from OpenBao via ESO userlist): $(grep '^chat|' "$RUN/pgbouncer-databases.txt" | cut -d'|' -f1-4)"
else
  rec LZ-11 FAIL "PgBouncer stats console: $(tail -1 "$RUN/pgbouncer-databases.txt")"
fi
kill $PF 2>/dev/null || true; unset PGB_STATS

# Gateway API
# The GKE controller rewrites the conditions during each SYNC (e.g. right after up.sh re-applies), so wait for
# Programmed=True up to 3 minutes before reading the status; a timeout is recorded as FAIL below.
k -n bravo-edge wait --for=condition=Programmed gateway/bravo-gateway --timeout=180s >>"$RUN/kubectl.err" 2>&1 || true
k -n bravo-edge get gateway bravo-gateway -o json >"$RUN/gateway.json" 2>>"$RUN/kubectl.err" || true
check LZ-12 "Gateway bravo-gateway (gke-l7-rilb) Programmed at $(jq -r '.status.addresses[0].value // "none"' "$RUN/gateway.json") = reserved $GATEWAY_IP" -- \
  bash -c "jq -e '([.status.conditions[]? | select(.type == \"Programmed\" and .status == \"True\")] | length == 1) and .status.addresses[0].value == \"$GATEWAY_IP\"' '$RUN/gateway.json'"
k -n chat get httproute chat -o json >"$RUN/httproute.json" 2>>"$RUN/kubectl.err" || true
check LZ-13 "HTTPRoute chat Accepted + ResolvedRefs" -- \
  jcheck "$RUN/httproute.json" '[.status.parents[]?.conditions[]? | select((.type == "Accepted" or .type == "ResolvedRefs") and .status == "True")] | length == 2'
gcloud compute forwarding-rules list --project "$PROJECT" --format=json >"$RUN/forwarding-rules.json"
check LZ-14 "load balancer is internal only (INTERNAL_MANAGED), no external forwarding rule" -- \
  jcheck "$RUN/forwarding-rules.json" 'length >= 1 and all(.[]; .loadBalancingScheme == "INTERNAL_MANAGED")'
# The Gateway makes one backend service per routed Service (plus a serve404 default): pick chat/chat-bravo-demo.
BS="$(gcloud compute backend-services list --project "$PROJECT" --regions "$REGION" --format='value(name)' 2>/dev/null | grep -- '-chat-chat-bravo-demo-' | head -1)"
if [[ -n $BS ]]; then
  gcloud compute backend-services get-health "$BS" --region "$REGION" --project "$PROJECT" --format=json >"$RUN/backend-health.json" 2>&1 || true
  check LZ-15 "LB backend health ($BS): $(jq -r '[.[].status.healthStatus[]?.healthState] | join(",")' "$RUN/backend-health.json" 2>/dev/null)" -- \
    jcheck "$RUN/backend-health.json" '[.[].status.healthStatus[]?.healthState] | length >= 1 and all(. == "HEALTHY")'
else
  rec LZ-15 FAIL "no regional backend service found"
fi

# Cloudflare edge
check LZ-16 "cf-edge/cloudflared ready (image $CLOUDFLARED_IMAGE)" -- k -n cf-edge rollout status deploy/cloudflared --timeout=30s
TUNNEL_ID="$(terraform -chdir="$REPO_ROOT/infra/envs/edge" output -json tunnel_ids 2>/dev/null | jq -r '.gke // empty')"
if [[ -n $TUNNEL_ID ]]; then
  "$PILOT_DIR/scripts/keys.sh" with-cloudflare -- bash -c 'printf "header = \"Authorization: Bearer %s\"\n" "$CLOUDFLARE_API_TOKEN" | curl -sS --max-time 20 -K - "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/'"$TUNNEL_ID"'"' \
    | jq '{success, status: .result.status, name: .result.name, connections: [.result.connections[]? | {colo_name, origin_ip, opened_at}]}' >"$RUN/tunnel.json" 2>&1 || true
  check LZ-17 "Cloudflare tunnel $(jq -r .name "$RUN/tunnel.json" 2>/dev/null) status $(jq -r .status "$RUN/tunnel.json" 2>/dev/null), $(jq '.connections | length' "$RUN/tunnel.json" 2>/dev/null) edge connection(s)" -- \
    jcheck "$RUN/tunnel.json" '.status == "healthy" and (.connections | length) >= 1'
else
  rec LZ-17 UNKNOWN "edge output tunnel_ids.gke missing"
fi
HC="$(curl -sS -o "$RUN/public-healthz.txt" -w '%{http_code}' --max-time 15 "https://$PUBLIC_HOSTNAME/healthz" 2>>"$RUN/public.err" || true)"
check LZ-18 "public https://$PUBLIC_HOSTNAME/healthz -> HTTP $HC through Cloudflare -> cf-edge -> Gateway -> app" -- test "$HC" = 200
curl -sS --max-time 15 "https://$PUBLIC_HOSTNAME/api/status" >"$RUN/public-status.json" 2>>"$RUN/public.err" || true
check LZ-19 "app reports env $(jq -r '.env.id // "?"' "$RUN/public-status.json" 2>/dev/null) ($(jq -r '.env.label // "?"' "$RUN/public-status.json" 2>/dev/null)), instance $(jq -r '.instance // "?"' "$RUN/public-status.json" 2>/dev/null)" -- \
  jcheck "$RUN/public-status.json" '.env.id == "gcp"'
check LZ-20 "app image is the source digest $DEMO_DIGEST, 2 replicas ready" -- \
  bash -c "kubectl --context '$CLUSTER' -n chat get deploy chat-bravo-demo -o json | jq -e '(.spec.template.spec.containers[0].image | endswith(\"@$DEMO_DIGEST\")) and .status.readyReplicas == 2'"
# DB path of the app: the poller reads messages through PgBouncer; poll_age_ms is null until a poll succeeds.
check LZ-21 "app DB path through PgBouncer ($ACTIVE): poll_age_ms=$(jq -r '.poll_age_ms' "$RUN/public-status.json" 2>/dev/null)" -- \
  jcheck "$RUN/public-status.json" '.poll_age_ms != null and .poll_age_ms < 10000'

# Observability
check LZ-22 "OTel agent DaemonSet ready ($OTEL_IMAGE)" -- k -n bravo-observability rollout status ds/otel-agent --timeout=30s
OTEL_POD="$(k -n bravo-observability get pod -l app.kubernetes.io/name=otel-agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
LP="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
k -n bravo-observability port-forward "pod/$OTEL_POD" "$LP:8888" >"$RUN/pf-otel.log" 2>&1 &
PF=$!; sleep 3
curl -sS --max-time 5 "http://127.0.0.1:$LP/metrics" >"$RUN/otel-metrics.txt" 2>&1 || true
kill $PF 2>/dev/null || true
SENT="$(awk '/^otelcol_exporter_sent_(spans|metric_points|log_records)/ {s+=$2} END {print s+0}' "$RUN/otel-metrics.txt")"
FAILED="$(awk '/^otelcol_exporter_send_failed_(spans|metric_points|log_records)/ {s+=$2} END {print s+0}' "$RUN/otel-metrics.txt")"
RECV="$(awk '/^otelcol_receiver_accepted_(spans|metric_points|log_records)/ {s+=$2} END {print s+0}' "$RUN/otel-metrics.txt")"
check LZ-23 "OTel agent -> platform Collector over the link: received $RECV, sent $SENT, failed $FAILED" -- \
  bash -c "test '$SENT' -gt 0 && test '$FAILED' -eq 0"
"$TG_SCRIPTS/links.sh" status >"$RUN/links.txt" 2>&1 || true
while read -r line; do
  name="${line%%:*}"
  case $line in *"running pid"*) rec "LINK" PASS "$name: ${line#*: }" ;; *) rec "LINK" FAIL "$line" ;; esac
done <"$RUN/links.txt"

# ------------------------------------------------------------------------------------------------ controls
# NET-01: NetworkPolicy enforcement (Dataplane V2). Negative from an unrelated namespace, positive control
# from a pod with the app's labels in chat. Same busybox digest, 3 s connect timeout.
nettest() {  # nettest NS NAME LABELS HOST PORT
  k -n "$1" run "$2" --image="$NETTEST_IMAGE" --restart=Never --labels="$3" --quiet \
    --overrides='{"spec":{"automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"runAsUser":65534,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"'"$2"'","image":"'"$NETTEST_IMAGE"'","command":["nc","-z","-w","3","'"$4"'","'"$5"'"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' >/dev/null 2>&1
  local phase=""
  for _ in $(seq 1 40); do
    phase="$(k -n "$1" get pod "$2" -o jsonpath='{.status.phase}' 2>/dev/null)"
    [[ $phase == Succeeded || $phase == Failed ]] && break
    sleep 2
  done
  k -n "$1" delete pod "$2" --wait=false >/dev/null 2>&1
  printf '%s' "$phase"
}
P1="$(nettest default nettest-db "bravo.io/test=net01" chat-db-rw.chat.svc.cluster.local 5432)"
check NET-01a "pod in ns default -> chat-db-rw:5432 blocked (nc result: $P1)" -- test "$P1" = Failed
P2="$(nettest default nettest-pgb "bravo.io/test=net01" chat-pgbouncer.chat.svc.cluster.local 6432)"
check NET-01b "pod in ns default -> chat-pgbouncer:6432 blocked (nc result: $P2)" -- test "$P2" = Failed
P3="$(nettest chat nettest-app "app.kubernetes.io/name=bravo-demo,app.kubernetes.io/instance=chat" chat-pgbouncer.chat.svc.cluster.local 6432)"
check NET-01c "control: pod with app labels in ns chat -> chat-pgbouncer:6432 allowed (nc result: $P3)" -- test "$P3" = Succeeded
P4="$(nettest chat nettest-app-db "app.kubernetes.io/name=bravo-demo,app.kubernetes.io/instance=chat" chat-db-rw.chat.svc.cluster.local 5432)"
check NET-01d "app-labelled pod -> chat-db-rw:5432 directly blocked (only via PgBouncer) (nc result: $P4)" -- test "$P4" = Failed
check NET-01e "no public entry point: nodes without external IP, API private + IAM DNS endpoint, LB internal (INF-05, INF-06, LZ-14)" -- \
  bash -c "jq -e '[.[].networkInterfaces[].accessConfigs[]?] | length == 0' '$RUN/instances.json' && jq -e 'all(.[]; .loadBalancingScheme == \"INTERNAL_MANAGED\")' '$RUN/forwarding-rules.json'"

# IAM-01 / ISO-01: another identity cannot read the app's secrets
check ISO-01 "ServiceAccount default/default cannot get Secret chat/bravo-demo-db" -- \
  bash -c "test \"\$(kubectl --context '$CLUSTER' auth can-i get secret/bravo-demo-db -n chat --as=system:serviceaccount:default:default)\" = no"
check ISO-02 "app ServiceAccount token not mounted (automountServiceAccountToken=false)" -- \
  bash -c "kubectl --context '$CLUSTER' -n chat get deploy chat-bravo-demo -o json | jq -e '.spec.template.spec.automountServiceAccountToken == false'"
gcloud storage buckets get-iam-policy "gs://$ATTACH_BUCKET" --project "$PROJECT" --format=json >"$RUN/bucket-iam.json"
check IAM-01 "bucket IAM: only chat-app has object access among service accounts; no allUsers/allAuthenticatedUsers" -- \
  jcheck "$RUN/bucket-iam.json" '([.bindings[].members[] | select(test("allUsers|allAuthenticatedUsers"))] | length == 0) and ([.bindings[] | select(.role | test("storage.object")) | .members[] | select(startswith("serviceAccount:"))] | unique == ["serviceAccount:'"$CHAT_APP_SA"'"])'

# SEC-01 / ENC-02: no secret value in Terraform state or in the repository; encryption keys are customer-managed
APP_AK_SK="$(bao GET "secret/data/envs/$BAO_ENV/object-store" "$T" | jq -r .data.data.secret_access_key)"
if tf state pull 2>/dev/null | APP_SK="$APP_AK_SK" python3 -I -c 'import os,sys; sys.exit(1 if os.environ["APP_SK"] in sys.stdin.read() else 0)'; then
  rec SEC-01a PASS "Terraform state (S3 backend) does not contain the GCS HMAC secret; no google_storage_hmac_key resource ($(tf state list | grep -c hmac || true) found)"
else
  rec SEC-01a FAIL "HMAC secret found in Terraform state"
fi
if grep -rlF --exclude-dir=.git --exclude-dir=.terraform --exclude-dir=node_modules --exclude-dir=.venv -f <(printf '%s\n' "$APP_AK_SK") "$REPO_ROOT" >/dev/null 2>&1; then
  rec SEC-01b FAIL "HMAC secret found in a repository file"
else
  rec SEC-01b PASS "repository files do not contain the GCS HMAC secret"
fi
unset APP_AK_SK
check ENC-02 "CMEK: GKE Secrets (gke-secrets), node/PV disks (pd), bucket default key (attachments)" -- \
  bash -c "jq -e '.databaseEncryption.state == \"ENCRYPTED\"' '$C' && jq -e '.config.bootDiskKmsKey | test(\"cryptoKeys/pd\")' '$NP' && jq -e '.default_kms_key | test(\"cryptoKeys/attachments\")' '$RUN/bucket.json'"

# AUD-01: Kubernetes API audit events are searchable (GKE Admin Activity audit logs, always on)
gcloud logging read 'logName="projects/'"$PROJECT"'/logs/cloudaudit.googleapis.com%2Factivity" AND protoPayload.serviceName="k8s.io" AND protoPayload.methodName:"secrets"' \
  --project "$PROJECT" --freshness=2h --limit=5 --format=json >"$RUN/audit-k8s.json" 2>"$RUN/audit-k8s.err" || true
check AUD-01 "K8s API audit (Cloud Audit Logs, Admin Activity) has Secret write events: $(jq -r '[.[].protoPayload.methodName] | unique | join(",")' "$RUN/audit-k8s.json" 2>/dev/null)" -- \
  jcheck "$RUN/audit-k8s.json" 'length >= 1'

# DR-01: CNPG backup/restore is not configured in this overlay yet.
rec DR-01 UNKNOWN "CNPG backup to object storage not configured in overlays/gke (no ScheduledBackup); restore not tested"

# ------------------------------------------------------------------------------------------------ OBJ-01..06 (GCS)
# The gate's own probes (platform/gate/src/bravo_gate/probes/objstore.py), same requests as on every engine.
OBJ_JSON="$RUN/obj.json"
(
  J="$(bao GET "secret/data/envs/$BAO_ENV/object-store" "$T")"
  GCS_APP_AK="$(printf '%s' "$J" | jq -r .data.data.access_key_id)"
  GCS_APP_SK="$(printf '%s' "$J" | jq -r .data.data.secret_access_key)"
  J="$(bao GET "secret/data/envs/$BAO_ENV/gate-other-s3" "$T")"
  GCS_OTHER_AK="$(printf '%s' "$J" | jq -r .data.data.access_key_id)"
  GCS_OTHER_SK="$(printf '%s' "$J" | jq -r .data.data.secret_access_key)"
  unset J
  export GCS_APP_AK GCS_APP_SK GCS_OTHER_AK GCS_OTHER_SK BUCKET="$ATTACH_BUCKET"
  "$REPO_ROOT/platform/gate/.venv/bin/python" -I - <<'PY'
import json, os
from bravo_gate.environments import Credential, ObjectStoreSection
from bravo_gate.probes.objstore import ObjectStoreProbe
s = ObjectStoreSection(
    bucket=os.environ["BUCKET"], region="auto", endpoint_url="https://storage.googleapis.com",
    addressing_style="path", canary_key="_gate/canary", write_probes=True, timeout_s=10.0,
    app_credential=Credential(access_key_env="GCS_APP_AK", secret_key_env="GCS_APP_SK"),
    other_credential=Credential(access_key_env="GCS_OTHER_AK", secret_key_env="GCS_OTHER_SK"),
)
p = ObjectStoreProbe(s, allow_writes=True)   # write probes approved for this store (OBJ-03, OBJ-04)
out = {name: getattr(p, name)().as_dict() for name in ("obj01", "obj02", "obj03", "obj04", "obj05", "obj06")}
print(json.dumps(out))
PY
) >"$OBJ_JSON" 2>"$RUN/obj.err" || true
for i in 1 2 3 4 5 6; do
  v="$(jq -r ".obj0$i.verdict // \"UNKNOWN\"" "$OBJ_JSON" 2>/dev/null || echo UNKNOWN)"
  r="$(jq -r ".obj0$i.reason // \"probe_not_run\"" "$OBJ_JSON" 2>/dev/null || echo probe_not_run)"
  e="$(jq -c ".obj0$i.evidence | {request, response, plain_endpoint, write} | with_entries(select(.value != null))" "$OBJ_JSON" 2>/dev/null || true)"
  rec "OBJ-0$i" "$v" "$r $e"
done
unset T

echo "== PASS $NPASS  FAIL $NFAIL  UNKNOWN $NUNK   evidence: $RUN"
jq -s '{pass: (map(select(.verdict=="PASS"))|length), fail: (map(select(.verdict=="FAIL"))|length), unknown: (map(select(.verdict=="UNKNOWN"))|length), results: .}' "$RESULTS" >"$RUN/summary.json"
