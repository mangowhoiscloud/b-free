#!/usr/bin/env bash
# Verify the landing zone on one cluster against what it claims (D-23, CLOUD-E2E-CONTRACT §3, §8, §9).
# Every line is a live observation: PASS, FAIL, or PENDING (known to wait on something outside
# this cluster, with the reason). Exit 1 if any FAIL.
#   verify.sh <overlay> <kube-context>
set -uo pipefail
overlay=${1:?usage: verify.sh <overlay> <kube-context>}
ctx=${2:?usage: verify.sh <overlay> <kube-context>}
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; . "$here/versions.env"; set +a
LIMA_SERVER=${BRAVO_LIMA_SERVER:-bravo-onprem-server}

k() { kubectl --context "$ctx" "$@"; }
fails=0
pass() { printf 'PASS     %-30s %s\n' "$1" "$2"; }
fail() { printf 'FAIL     %-30s %s\n' "$1" "$2"; fails=$((fails + 1)); }
pend() { printf 'PENDING  %-30s %s\n' "$1" "$2"; }
check() { local id=$1 detail=$2; shift 2; if "$@" >/dev/null 2>&1; then pass "$id" "$detail"; else fail "$id" "$detail"; fi; }

echo "# landing zone verify: overlay=$overlay context=$ctx at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- cluster -------------------------------------------------------------------------------------
nodes="$(k get nodes -o jsonpath='{range .items[*]}{.metadata.name}={.status.nodeInfo.kubeletVersion}/{.status.conditions[?(@.type=="Ready")].status}/{.status.addresses[?(@.type=="InternalIP")].address} {end}')"
[[ $(grep -o '/True/' <<<"$nodes" | wc -l | tr -d ' ') -ge 2 ]] && pass nodes.ready "$nodes" || fail nodes.ready "$nodes"

if [[ $overlay == k3s ]]; then
  cfg="$(limactl shell "$LIMA_SERVER" -- sudo cat /etc/rancher/k3s/config.yaml </dev/null 2>/dev/null)"
  for flag in "protect-kernel-defaults: true" "secrets-encryption: true" "flannel-backend: none" \
              "disable-kube-proxy: true" "admission-control-config-file=" "audit-policy-file="; do
    grep -qF "$flag" <<<"$cfg" && pass k3s.config "$flag" || fail k3s.config "$flag missing"
  done
  enc="$(limactl shell "$LIMA_SERVER" -- sudo k3s secrets-encrypt status </dev/null 2>/dev/null | tr '\n' ' ')"
  grep -q "Encryption Status: Enabled" <<<"$enc" && pass k3s.secrets-encryption "$enc" || fail k3s.secrets-encryption "$enc"
  al="$(limactl shell "$LIMA_SERVER" -- sudo sh -c 'wc -l < /var/lib/rancher/k3s/server/logs/audit.log' </dev/null 2>/dev/null | tr -d ' ')"
  [[ ${al:-0} -gt 0 ]] && pass k3s.audit-log "/var/lib/rancher/k3s/server/logs/audit.log: $al lines" || fail k3s.audit-log "no audit log"
fi

# PSA restricted default: a privileged pod in a namespace without labels must be rejected.
psa="$(k create namespace bravo-verify-psa --dry-run=server -o name 2>&1; \
       printf 'apiVersion: v1\nkind: Pod\nmetadata: {name: p, namespace: default}\nspec:\n  containers: [{name: c, image: busybox, securityContext: {privileged: true}}]\n' \
       | k apply --dry-run=server -f - 2>&1)"
grep -q 'violates PodSecurity "restricted' <<<"$psa" && pass psa.restricted-default "privileged pod in ns default rejected (server dry-run)" \
  || fail psa.restricted-default "$(tail -1 <<<"$psa")"

# --- network -------------------------------------------------------------------------------------
cst="$(k -n kube-system exec ds/cilium -c cilium-agent -- cilium-dbg status 2>/dev/null)"
grep -q "KubeProxyReplacement: *True" <<<"$cst" && pass cilium.kpr "$(grep KubeProxyReplacement <<<"$cst" | xargs)" || fail cilium.kpr "kube-proxy replacement off"
grep -q "Tunnel \[vxlan\]" <<<"$cst" && pass cilium.vxlan "$(grep '^Routing' <<<"$cst" | xargs)" || fail cilium.vxlan "not vxlan"
grep -q "^Hubble: *Ok" <<<"$cst" && pass hubble "$(grep '^Hubble' <<<"$cst" | xargs)" || fail hubble "hubble not ok"
check hubble-relay "deploy/hubble-relay available" k -n kube-system rollout status deploy/hubble-relay --timeout=5s
gw="$(k -n bravo-edge get gateway bravo-gateway -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status} {.status.addresses[0].value}' 2>/dev/null)"
[[ $gw == True* ]] && pass gateway.bravo-gateway "Programmed, address ${gw#True }; Service cilium-gateway-bravo-gateway" || fail gateway.bravo-gateway "${gw:-absent}"
hr="$(k -n chat get httproute chat -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null)"
[[ $hr == True ]] && pass httproute.chat "Accepted by bravo-gateway" || fail httproute.chat "${hr:-absent}"

# --- storage -------------------------------------------------------------------------------------
for sc in bravo-db bravo-general; do
  p="$(k get sc "$sc" -o jsonpath='{.provisioner}' 2>/dev/null)"
  [[ -n $p ]] && pass "storageclass.$sc" "provisioner $p" || fail "storageclass.$sc" absent
done

# --- data ----------------------------------------------------------------------------------------
db="$(k -n chat get cluster chat-db -o jsonpath='{.status.phase}|{.status.image}' 2>/dev/null)"
[[ $db == "Cluster in healthy state|"* ]] && pass cnpg.chat-db "${db/|/ image }" || fail cnpg.chat-db "${db:-absent}"
pg="$(k -n chat exec chat-db-1 -c postgres -- psql -Atc "select current_setting('server_version')||' wal_level='||current_setting('wal_level')||' max_replication_slots='||current_setting('max_replication_slots')||' max_slot_wal_keep_size='||current_setting('max_slot_wal_keep_size')" 2>/dev/null)"
[[ $pg == 16.*"wal_level=logical"* ]] && pass postgres.settings "$pg" || fail postgres.settings "${pg:-unreachable}"
check pgbouncer.ready "deploy/chat-pgbouncer available" k -n chat rollout status deploy/chat-pgbouncer --timeout=5s
act="$(k -n chat exec deploy/chat-pgbouncer -c pgbouncer -- sh -c 'for s in src dst; do cmp -s /etc/pgbouncer/targets/target-$s.ini /run/pgbouncer/active.ini && echo $s; done' 2>/dev/null)"
[[ -n $act ]] && pass pgbouncer.active-target "active.ini = target-$act.ini ($(k -n chat get cm chat-pgbouncer-targets -o jsonpath="{.data.target-$act\.ini}" | tail -1))" \
  || fail pgbouncer.active-target "active.ini matches no target"

# --- app -----------------------------------------------------------------------------------------
img="$(k -n chat get deploy chat-bravo-demo -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)"
rdy="$(k -n chat get deploy chat-bravo-demo -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null)"
[[ $img == *@sha256:* ]] && pass app.image-digest "$img" || fail app.image-digest "${img:-absent} (not pinned by digest)"
[[ ${rdy%%/*} -ge 1 ]] 2>/dev/null && pass app.ready "ready $rdy" || fail app.ready "ready ${rdy:-0}"
check app.admin-service "Service chat-admin (8081, ClusterIP)" sh -c "kubectl --context '$ctx' -n chat get svc chat-admin -o jsonpath='{.spec.type}{.spec.ports[0].port}' | grep -qx ClusterIP8081"
for s in chat-admin bravo-demo-db chat-pgbouncer-auth bravo-demo-s3; do
  check "secret.chat/$s" "present (value not read)" k -n chat get secret "$s"
done

# --- edge ----------------------------------------------------------------------------------------
cf="$(k -n cf-edge get deploy cloudflared -o jsonpath='{.status.readyReplicas}/{.spec.replicas} {.spec.template.spec.containers[0].image}' 2>/dev/null)"
[[ $cf == [1-9]* ]] && pass cf-edge.cloudflared "ready $cf" || fail cf-edge.cloudflared "${cf:-absent}"
conns="$(k -n cf-edge logs deploy/cloudflared --since=24h 2>/dev/null | grep -c 'Registered tunnel connection')"
[[ $conns -ge 1 ]] && pass cf-edge.tunnel "$conns tunnel connections registered (one pod's log)" || fail cf-edge.tunnel "no registered connection"

# --- policy --------------------------------------------------------------------------------------
kv="$(k get generatingpolicy bravo-default-deny -o jsonpath='{.status.conditionStatus.ready}' 2>/dev/null)"
[[ $kv == true ]] && pass kyverno.default-deny "GeneratingPolicy bravo-default-deny ready" || fail kyverno.default-deny "${kv:-absent}"
ns_t="bravo-verify-dd-$RANDOM"
k create namespace "$ns_t" >/dev/null 2>&1; k label namespace "$ns_t" bravo.dev/default-deny=enabled >/dev/null 2>&1
for _ in $(seq 1 20); do k -n "$ns_t" get networkpolicy default-deny >/dev/null 2>&1 && break; sleep 1; done
np="$(k -n "$ns_t" get networkpolicy default-deny -o jsonpath='{.spec.policyTypes}' 2>/dev/null)"
[[ $np == *Ingress*Egress* ]] && pass kyverno.generates "labelled ns $ns_t got NetworkPolicy default-deny $np" || fail kyverno.generates "no default-deny in $ns_t"
k delete namespace "$ns_t" --wait=false >/dev/null 2>&1
dd="$(k -n chat get networkpolicy -l bravo.dev/generated-by=kyverno-bravo-default-deny -o name 2>/dev/null)"
[[ -z $dd ]] && pend kyverno.chat-not-enrolled "ns chat has no default-deny: the S2 gap, fixed only through the change approval" \
  || pass kyverno.chat-enrolled "$dd"

# --- object store --------------------------------------------------------------------------------
if [[ $overlay == k3s ]]; then
  ch="$(k -n rook-ceph get cephcluster rook-ceph -o jsonpath='{.status.phase} {.status.ceph.health}' 2>/dev/null)"
  [[ $ch == Ready* ]] && pass rook.cephcluster "$ch" || fail rook.cephcluster "${ch:-absent}"
  osd="$(k -n rook-ceph get pods -l app=rook-ceph-osd --field-selector=status.phase=Running -o name 2>/dev/null | wc -l | tr -d ' ')"
  [[ $osd -ge 2 ]] && pass rook.osds "$osd OSD pods running" || fail rook.osds "$osd OSD pods running"
  os="$(k -n rook-ceph get cephobjectstore bravo -o jsonpath='{.status.phase}' 2>/dev/null)"
  [[ $os == Ready ]] && pass rgw.objectstore "CephObjectStore bravo Ready, Service rook-ceph-rgw-bravo 80/443" || fail rgw.objectstore "${os:-absent}"
  obc="$(k -n chat get objectbucketclaim bravo-demo-attachments -o jsonpath='{.status.phase}' 2>/dev/null)"
  [[ $obc == Bound ]] && pass rgw.bucket "OBC bravo-demo-attachments Bound" || fail rgw.bucket "${obc:-absent}"
  ot="$(k -n chat logs job/bravo-obj-tests 2>/dev/null | grep '^OBJ-0' | awk '{print $1$2}' | tr '\n' ' ')"
  if [[ -z $ot ]]; then pend rgw.obj-tests "no OBJ run in the last hour: scripts/obj-tests.sh $ctx"
  elif grep -q FAIL <<<"$ot"; then fail rgw.obj-tests "$ot"; else pass rgw.obj-tests "$ot(last run of scripts/obj-tests.sh)"; fi
fi

# --- secrets, telemetry --------------------------------------------------------------------------
check eso.controller "deploy/external-secrets available" k -n external-secrets rollout status deploy/external-secrets --timeout=5s
ss="$(k get clustersecretstore bravo-platform-openbao -o jsonpath='{.metadata.labels.bravo\.dev/link} {.status.conditions[0].status} {.status.conditions[0].reason}' 2>/dev/null)"
[[ $ss == pending* ]] && pend eso.store "ClusterSecretStore bravo-platform-openbao: link=$ss (D-16 link not built; secrets piped by keys.sh)" \
  || { [[ $ss == *True* ]] && pass eso.store "$ss" || fail eso.store "${ss:-absent}"; }
check otel.agent "deploy/otel-agent available (bravo-observability, 4317/4318)" k -n bravo-observability rollout status deploy/otel-agent --timeout=5s
pend otel.export "otel-agent exports to its debug log only until the link to the platform Collector exists"

# --- reachability from a pod (restricted PSA) ----------------------------------------------------
probe="bravo-verify-probe-$RANDOM"
k apply -f - >/dev/null 2>&1 <<YAML
apiVersion: v1
kind: Pod
metadata: {name: $probe, namespace: cf-edge}
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  securityContext: {runAsNonRoot: true, runAsUser: 65532, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: probe
      image: docker.io/curlimages/curl:8.17.0
      command:
        - sh
        - -c
        - |
          curl -s -o /dev/null -w "gateway %{http_code}\n" -H "Host: b-free-chat-dst.mangowhois.ai" --max-time 5 http://cilium-gateway-bravo-gateway.bravo-edge.svc.cluster.local/healthz
          echo "dns \$(nslookup host.lima.internal 2>/dev/null | awk '/^Name:/ {n=1} n && /^Address:/ {print \$2; exit}')"
          curl -s -o /dev/null -w "link15434 connect=%{time_connect}\n" --max-time 2 telnet://host.lima.internal:15434 </dev/null
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
YAML
for _ in $(seq 1 60); do
  ph="$(k -n cf-edge get pod "$probe" -o jsonpath='{.status.phase}' 2>/dev/null)"
  [[ $ph == Succeeded || $ph == Failed ]] && break; sleep 1
done
out="$(k -n cf-edge logs "$probe" 2>/dev/null)"
k -n cf-edge delete pod "$probe" --wait=false >/dev/null 2>&1
grep -q "gateway 200" <<<"$out" && pass edge.gateway-to-app "cf-edge -> Gateway (Host b-free-chat-dst) -> app /healthz 200" || fail edge.gateway-to-app "$(head -1 <<<"$out")"
if [[ $overlay == k3s ]]; then
  dns="$(sed -n 's/^dns //p' <<<"$out")"
  [[ -n $dns ]] && pass link.host-lima-internal "pod resolves host.lima.internal -> $dns (the laptop through Lima user-v2)" \
    || fail link.host-lima-internal "host.lima.internal does not resolve in a pod"
  ct="$(sed -n 's/^link15434 connect=//p' <<<"$out")"
  if [[ -n $ct && $ct != 0 && $ct != 0.000000 ]]; then
    pass link.src-db-port "pod -> host.lima.internal:15434 TCP connected in ${ct}s (temporary link (D-16 전), PgBouncer target-src)"
  else
    pend link.src-db-port "host.lima.internal:15434 not accepting: the platform's source-DB port-forward is not up"
  fi
fi

echo "# result: $fails FAIL"
((fails == 0))
