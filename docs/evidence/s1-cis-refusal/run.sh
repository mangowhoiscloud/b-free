#!/usr/bin/env bash
# Spec S1 exception 4a and the S1 acceptance condition, run for real on one throwaway Lima VM.
# Never touches the bravo-onprem VMs or ~/.kube/config (scratch kubeconfig under $WORK).
#   run.sh refuse   # empty Ubuntu VM + common cloud-init with cis_preconditions = false -> RKE2 profile: cis refuses
#   run.sh refuse-result  # only the result and diagnostic part of refuse, on the same VM
#   run.sh fix      # same VM: rke2-uninstall, common cloud-init as shipped (conditions back), reinstall,
#                   # landing-zone Cilium (cni: none), node Ready, kube-bench rke2-cis-1.9
#   run.sh accept   # S1 acceptance alone: a new empty VM + common cloud-init as shipped -> RKE2 CIS, node Ready
#   run.sh rerun    # same VM, after fix: run the common path again on the running node; no stop or start
#   run.sh clean    # delete the VM and the scratch kubeconfig
# BRAVO_CIS_DEMO_KUBE_BENCH=0 skips kube-bench in fix.
# Prints commands and outputs with KST times; the caller tees them into this folder (README.md).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
mod="$repo/infra/modules/cluster/onprem"
envtf="$repo/infra/envs/target-onprem/main.tf"
VM=${BRAVO_CIS_DEMO_VM:-bravo-cis-demo}
PORT=${BRAVO_CIS_DEMO_PORT:-36443}
CTX=$VM
WORK=${BRAVO_CIS_DEMO_WORK:-${TMPDIR:-/tmp}/bravo-cis-demo}
LIMACTL=${LIMACTL:-limactl}
[[ $VM != bravo-onprem* ]] || { echo "run.sh: refusing to use $VM (live on-prem VM)" >&2; exit 1; }
mkdir -p "$WORK"; chmod 700 "$WORK"
export KUBECONFIG="$WORK/kubeconfig" KUBECONFIG_TARGET="$WORK/kubeconfig"

ts() { TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST'; }
step() { printf '\n## %s  %s\n' "$(ts)" "$*"; }
# Mask anything token-shaped (none is expected: the agent token never leaves the VM).
mask() { sed -E -e 's/K10[0-9a-f]{16,}[^ ]*/K10***MASKED***/g' -e 's/^( *(token|password|secret): *)[^ ]+$/\1***MASKED***/'; }
# Laptop command / command inside the VM, echoed first.
# The command line goes to stderr so `run ... | tail` still shows it.
run() { local c="${*//"$repo"\//}"; printf '$ %s\n' "${c//"$WORK"/\$WORK}" >&2; "$@" 2>&1 | mask; return "${PIPESTATUS[0]}"; }
vsh() { printf '$ [%s] %s\n' "$VM" "$1"; "$LIMACTL" shell "$VM" -- sh -c "$1" </dev/null 2>&1 | mask; return "${PIPESTATUS[0]}"; }
# rke2-server journal in KST; optional first argument: only entries since that epoch second.
journal() { "$LIMACTL" shell "$VM" -- sudo env TZ=Asia/Seoul journalctl -u rke2-server -o short-iso --no-pager ${1:+--since "@$1"} </dev/null; }

# Same pins as infra/envs/target-onprem/main.tf (read from it, not copied).
img_url=$(awk '/image = \{/{f=1} f && /url/{gsub(/.*= "|"$/,""); print; exit}' "$envtf")
img_sha=$(awk '/image = \{/{f=1} f && /sha256/{gsub(/.*= "|"$/,""); print; exit}' "$envtf")
rke2_ver=$(awk '/rke2 = \{/{f=1} f && /version/{gsub(/.*= "|"$/,""); print; exit}' "$envtf")
rke2_sha=$(awk '/rke2 = \{/{f=1} f && /sha256/{gsub(/.*= "|"$/,""); print; exit}' "$envtf")

# Render the common cloud-init twice through the module: cis_preconditions = false (demo) and the default.
render() {
  local r="$WORK/render"; mkdir -p "$r"
  cat > "$r/main.tf" <<EOF
locals { rke2 = { version = "$rke2_ver", sha256 = "$rke2_sha" } }
module "no_cis" {
  source            = "$mod/cloud-init"
  role              = "server"
  rke2              = local.rke2
  node_name         = "$VM"
  cis_preconditions = false
}
module "cis" {
  source    = "$mod/cloud-init"
  role      = "server"
  rke2      = local.rke2
  node_name = "$VM"
}
output "no_cis" { value = module.no_cis.user_data }
output "cis" { value = module.cis.user_data }
EOF
  rm -rf "$r/.terraform"   # an absolute module source is copied at init: always copy the current module
  terraform -chdir="$r" init -input=false >/dev/null
  terraform -chdir="$r" apply -auto-approve -input=false >/dev/null
  terraform -chdir="$r" output -raw no_cis > "$WORK/cloud-config-no-cis.yaml"
  terraform -chdir="$r" output -raw cis > "$WORK/cloud-config-cis.yaml"
}

lima_yaml() {
  cat > "$WORK/$VM.yaml" <<EOF
# Throwaway VM for the S1 4a run: same vmType, arch, image and network as the bravo-onprem VMs
# (infra/modules/cluster/onprem/files/lima-node.yaml.tftpl), no Ceph data disk, API on 127.0.0.1:$PORT.
minimumLimaVersion: 1.1.0
vmType: vz
arch: aarch64
cpus: 4
memory: 6GiB
disk: 30GiB
images:
  - location: $img_url
    arch: aarch64
    digest: sha256:$img_sha
mounts: []
containerd: {system: false, user: false}
networks:
  - lima: user-v2
portForwards:
  - {guestPort: 6443, hostPort: $PORT, hostIP: 127.0.0.1}
  - {guestPortRange: [1, 65535], ignore: true}
ssh: {forwardAgent: false, loadDotSSHPubKeys: false}
EOF
}

CIS_KEYS="vm.panic_on_oom vm.overcommit_memory kernel.panic kernel.panic_on_oops"
host_state() {
  vsh 'echo "etcd user: $(id etcd 2>&1)"; echo "etcd group: $(getent group etcd || echo none)"'
  vsh "sysctl $CIS_KEYS"
}

empty_vm() {
  step "empty Ubuntu VM $VM (image $img_url)"
  lima_yaml
  if ! "$LIMACTL" list --json 2>/dev/null | jq -e --arg n "$VM" 'select(.name == $n)' >/dev/null; then
    run "$LIMACTL" create --tty=false --name "$VM" "$WORK/$VM.yaml" | tail -n 2
  fi
  run "$LIMACTL" start --tty=false --timeout 15m "$VM" | tail -n 1
  vsh '. /etc/os-release; echo "$PRETTY_NAME $(uname -m) kernel $(uname -r)"'
  vsh 'command -v rke2 || echo "rke2: not installed"; ls /etc/rancher 2>&1 || true'
  host_state
}

cmd_refuse() {
  step "render the common cloud-init (module infra/modules/cluster/onprem/cloud-init, RKE2 $rke2_ver)"
  render
  echo "cis_preconditions = false vs default (what the demo leaves out):"
  diff -u --label cloud-config-cis.yaml --label cloud-config-no-cis.yaml "$WORK/cloud-config-cis.yaml" "$WORK/cloud-config-no-cis.yaml" || true

  empty_vm
  "$LIMACTL" shell "$VM" -- sysctl -e $CIS_KEYS </dev/null | tr -d ' ' > "$WORK/sysctl-before.txt"

  step "install with the common path (scripts/rke2-node.sh server), cis_preconditions = false"
  local t0 rc=0; t0=$(date +%s)
  run "$mod/scripts/rke2-node.sh" server "$VM" "$WORK/cloud-config-no-cis.yaml" "$PORT" || rc=$?
  echo "rke2-node.sh exit code: $rc after $(( $(date +%s) - t0 )) s"
  refuse_result
}

# Read-only except the labelled diagnostic; can be re-run on its own (run.sh refuse-result).
refuse_result() {
  step "result: rke2-server state and the CIS refusal in the journal"
  vsh 'systemctl show rke2-server -p ActiveState -p SubState -p Result -p NRestarts'
  host_state
  echo "refusals so far: $(journal | grep -c 'level=fatal')  (systemd retries every 5 s: Restart=always)"
  echo "first refusal (journal, TZ=Asia/Seoul):"
  journal > "$WORK/journal.txt"   # to a file first: grep -m1 on the pipe would cut limactl shell (exit 255)
  grep -m1 -B1 -A3 'level=fatal' "$WORK/journal.txt" | mask
  echo "systemctl enable --now on its own (systemd 255 on Ubuntu 24.04; why the bootstrap runs enable and start apart):"
  vsh 'sudo systemctl enable --now rke2-server.service; echo "systemctl enable --now exit code: $?"'

  step "diagnostic only (by hand, not the fix path): set the CIS sysctls without the etcd user"
  vsh "sudo sysctl -w vm.overcommit_memory=1 kernel.panic=10 kernel.panic_on_oops=1"
  sleep 15
  echo "next refusals (journal, TZ=Asia/Seoul):"
  journal | grep 'level=fatal' | tail -n 2 | mask
  echo "undo the hand change (values measured before the install):"
  vsh "sudo sysctl -w $(tr '\n' ' ' < "$WORK/sysctl-before.txt")"
}

# RKE2 up with the CIS profile: host conditions, config, process users and flags, refusals since $1,
# then the landing-zone Cilium (RKE2 runs with cni: none) and node Ready.
cis_up() {
  step "CIS profile in effect"
  host_state
  vsh 'sudo cat /etc/rancher/rke2/config.yaml /etc/rancher/rke2/config.yaml.d/10-node.yaml'
  vsh 'ps -eo user:10,comm | awk "\$2==\"etcd\" || \$2==\"kube-apiserver\" || \$2==\"kubelet\"" | sort -u'
  vsh "ps -o args= -C kube-apiserver | tr ' ' '\n' | grep -E '^--(admission-control-config-file|audit-policy-file|audit-log-path|encryption-provider-config|anonymous-auth|profiling)='"
  vsh 'systemctl show rke2-server -p ActiveState -p SubState -p NRestarts'
  echo "refusals since the install started: $(journal "$1" | grep -c 'level=fatal' || true)"
  vsh 'sudo ls /var/lib/rancher/rke2/server/manifests/'

  step "kubeconfig (scratch file, ~/.kube/config untouched)"
  run "$mod/scripts/kubeconfig.sh" add "$VM" "$CTX" "$PORT"
  run kubectl --context "$CTX" get node -o wide

  step "landing-zone Cilium (RKE2 runs with cni: none): deploy/landing-zone/install.sh rke2 <ctx> cilium"
  run "$repo/deploy/landing-zone/install.sh" rke2 "$CTX" cilium
  run kubectl --context "$CTX" get node -o wide
  run kubectl --context "$CTX" get pods -A
  echo "packaged Traefik CRD chart (skipped by the common cloud-init) and the landing-zone Gateway API CRDs:"
  run kubectl --context "$CTX" -n kube-system get helmcharts.helm.cattle.io
  echo "gateway.networking.k8s.io CRDs: $(kubectl --context "$CTX" get crd -o name | grep -c 'gateway.networking.k8s.io')"
}

# Re-run the common path on the running server (what a target-onprem apply does after a cloud-init change):
# the unit must keep its process (no stop, no start).
cmd_rerun() {
  [[ -s $WORK/cloud-config-cis.yaml ]] || render
  local before after t0
  step "re-run the common path on the running node: rke2-node.sh server again, same document"
  before=$(vsh 'systemctl show rke2-server -p MainPID -p ActiveEnterTimestamp -p NRestarts' | tail -n +2 | sort | tr '\n' ' ')
  echo "before: $before"
  t0=$(date +%s)
  run "$mod/scripts/rke2-node.sh" server "$VM" "$WORK/cloud-config-cis.yaml" "$PORT"
  after=$(vsh 'systemctl show rke2-server -p MainPID -p ActiveEnterTimestamp -p NRestarts' | tail -n +2 | sort | tr '\n' ' ')
  echo "after:  $after"
  [[ $before == "$after" ]] && echo "same process, no restart: yes" || echo "same process, no restart: NO"
  echo "rke2-server stop or start lines in the journal since the re-run: $(journal "$t0" | grep -c -E 'Stopping rke2-server|Started rke2-server|Starting rke2-server' || true)"
}

# S1 acceptance on a VM that never had RKE2: the common cloud-init as shipped, nothing else.
cmd_accept() {
  step "render the common cloud-init (module infra/modules/cluster/onprem/cloud-init, RKE2 $rke2_ver, defaults)"
  render
  empty_vm
  step "install with the common path (scripts/rke2-node.sh server), common cloud-init as shipped"
  local t0; t0=$(date +%s)
  run "$mod/scripts/rke2-node.sh" server "$VM" "$WORK/cloud-config-cis.yaml" "$PORT"
  echo "install to API ready: $(( $(date +%s) - t0 )) s"
  cis_up "$t0"
}

cmd_fix() {
  [[ -s $WORK/cloud-config-cis.yaml ]] || render
  step "fix: common cloud-init as shipped (cis_preconditions default true): CIS sysctls and etcd user back"
  diff -u --label cloud-config-no-cis.yaml --label cloud-config-cis.yaml "$WORK/cloud-config-no-cis.yaml" "$WORK/cloud-config-cis.yaml" || true
  host_state

  step "reinstall on the same VM: rke2-uninstall.sh, then the common path again"
  vsh 'sudo /usr/local/bin/rke2-uninstall.sh >/dev/null 2>&1; echo "uninstall exit code: $?"; command -v rke2 || echo "rke2: removed"; ls /etc/rancher/rke2 2>&1 || true'
  local t0; t0=$(date +%s)
  run "$mod/scripts/rke2-node.sh" server "$VM" "$WORK/cloud-config-cis.yaml" "$PORT"
  echo "install to API ready: $(( $(date +%s) - t0 )) s"

  cis_up "$t0"

  [[ ${BRAVO_CIS_DEMO_KUBE_BENCH:-1} == 1 ]] || return 0
  step "kube-bench rke2-cis-1.9 (deploy/platform/kube-bench/job-rke2.yaml, same job as the platform run)"
  local f; f="$here/kube-bench-$VM-$(date -u +%Y%m%dT%H%M%SZ).json"
  kubectl --context "$CTX" apply -f "$repo/deploy/platform/kube-bench/job-rke2.yaml" >/dev/null
  kubectl --context "$CTX" -n kube-bench wait --for=condition=complete job/kube-bench-rke2 --timeout=300s >/dev/null || \
    kubectl --context "$CTX" -n kube-bench wait --for=condition=failed job/kube-bench-rke2 --timeout=10s >/dev/null || true
  kubectl --context "$CTX" -n kube-bench logs job/kube-bench-rke2 | sed -n '/^{/,$p' > "$f"
  kubectl --context "$CTX" delete ns kube-bench --wait=false >/dev/null
  jq -r '"benchmark rke2-cis-1.9 (approx. for RKE2 v1.36): pass=\(.Totals.total_pass) fail=\(.Totals.total_fail) warn=\(.Totals.total_warn) info=\(.Totals.total_info)"' "$f"
  jq -r '.Controls[] | .tests[] | .results[] | select(.status=="FAIL") | "FAIL \(.test_number) \(.test_desc)"' "$f"
  echo "saved ${f#"$repo"/}"
}

cmd_clean() {
  step "delete the throwaway VM $VM and the scratch kubeconfig"
  run "$LIMACTL" delete --force "$VM" | tail -n 1
  rm -rf "$WORK"
  run "$LIMACTL" list
}

case ${1:-} in
  refuse) cmd_refuse ;;
  refuse-result) refuse_result ;;
  accept) cmd_accept ;;
  rerun) cmd_rerun ;;
  fix) cmd_fix ;;
  clean) cmd_clean ;;
  *) echo "usage: run.sh refuse|refuse-result|fix|accept|rerun|clean" >&2; exit 2 ;;
esac
