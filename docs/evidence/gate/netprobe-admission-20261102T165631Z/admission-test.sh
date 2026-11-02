#!/usr/bin/env bash
# ValidatingAdmissionPolicy bravo-gate-netprobe: server-side dry-run Pod creates in ns chat AS the gate identity of each
# cluster (kubectl impersonation by the operator). Nothing is created (--dry-run=server runs admission, then discards).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"
GKE_KUBECONFIG="$REPO/infra/envs/target-gcp/.terraform/bravo/kubeconfig"
OTHER_IMAGE="docker.io/library/busybox@sha256:0000000000000000000000000000000000000000000000000000000000000001"
pod() { # image automount deadline label
  jq -n --arg img "$1" --argjson auto "$2" --argjson dl "$3" --arg lbl "$4" '{apiVersion: "v1", kind: "Pod",
    metadata: {generateName: "gate-netprobe-", namespace: "chat",
      labels: ({"app.kubernetes.io/name": "bravo-demo", "app.kubernetes.io/instance": "chat"} + (if $lbl == "" then {} else {"bravo.io/probe": $lbl} end))},
    spec: {restartPolicy: "Never", activeDeadlineSeconds: $dl, automountServiceAccountToken: $auto, enableServiceLinks: false,
      securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: "RuntimeDefault"}},
      containers: [{name: "probe", image: $img, command: ["python", "-c", "print(1)"],
        securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, runAsNonRoot: true,
          capabilities: {drop: ["ALL"]}, seccompProfile: {type: "RuntimeDefault"}},
        resources: {requests: {cpu: "10m", memory: "32Mi"}, limits: {memory: "96Mi"}}}]}}'
}
try() { # label ctx-args... -- pod-json on stdin
  local name=$1; shift
  local out; out="$("$@" create --dry-run=server -o name -f - 2>&1)"; local rc=$?
  printf '%-58s rc=%s %s\n' "$name" "$rc" "$(printf '%s' "$out" | tr '\n' ' ' | sed 's/Error from server (Forbidden): error when creating "STDIN": //' | cut -c1-230)"
}
run_env() { # env app-image kubectl-args...
  local env=$1 img=$2; shift 2
  echo "== $env ($(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST'))"
  pod "$img" false 90 gate-net-paths | try "gate: app image, compliant (allowed)" "$@"
  pod "$OTHER_IMAGE" false 90 gate-net-paths | try "gate: other image (denied)" "$@"
  pod "$img" true 90 gate-net-paths | try "gate: automountServiceAccountToken true (denied)" "$@"
  pod "$img" false 300 gate-net-paths | try "gate: activeDeadlineSeconds 300 (denied)" "$@"
  pod "$img" false 90 "" | try "gate: no probe label (denied)" "$@"
}
SRC_IMG=$(kubectl --context bravo-source -n chat get deploy chat-bravo-demo -o jsonpath='{.spec.template.spec.containers[0].image}')
ONP_IMG=$(kubectl --context bravo-onprem -n chat get deploy chat-bravo-demo -o jsonpath='{.spec.template.spec.containers[0].image}')
GKE_IMG=$(KUBECONFIG="$GKE_KUBECONFIG" kubectl --context bravo-target-gcp -n chat get deploy chat-bravo-demo -o jsonpath='{.spec.template.spec.containers[0].image}')
run_env source-aws "$SRC_IMG" kubectl --context bravo-source --as=bravo-gate-admission-test --as-group=bravo-gate --as-group=system:authenticated
run_env target-onprem "$ONP_IMG" kubectl --context bravo-onprem --as=system:serviceaccount:kube-system:bravo-gate \
  --as-group=system:serviceaccounts --as-group=system:serviceaccounts:kube-system --as-group=system:authenticated
run_env target-gcp "$GKE_IMG" env KUBECONFIG="$GKE_KUBECONFIG" kubectl --context bravo-target-gcp \
  --as=bravo-gate-ro@b-free-target-20261102.iam.gserviceaccount.com --as-group=system:authenticated
echo "== scope: the policy matches only the gate identity (operator, other image, dry-run)"
pod "$OTHER_IMAGE" false 90 gate-net-paths | try "operator on-prem: other image (policy not matched)" kubectl --context bravo-onprem
