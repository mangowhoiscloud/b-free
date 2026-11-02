#!/usr/bin/env bash
# Shared helpers for the cloud E2E scripts (sourced). Settings: deploy/cloud/cloud.env.
set -Eeuo pipefail
CLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CLOUD_DIR/../.." && pwd)"
RUNS="$REPO/deploy/pilot/runs"
STATE="$RUNS/cloud-state"   # pid files, status files, rendered topology (no secrets)
mkdir -p "$STATE"
[ -f "$CLOUD_DIR/cloud.env" ] || { echo "cloud: copy cloud.env.example to cloud.env" >&2; exit 2; }
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  if [ -z "${!key+x}" ]; then export "$key=${line#*=}"; fi
done < "$CLOUD_DIR/cloud.env"
BAO_SH="$REPO/deploy/pilot/scripts/executor-bao.sh"
log()  { echo "[cloud $(date +%H:%M:%S)] $*"; }
fail() { echo "[cloud $(date +%H:%M:%S)] FAIL: $*" >&2; exit 1; }
ctx_ok() { kubectl config get-contexts -o name | grep -qx "$1"; }

# ops <args...>: python -m bravo_executor.ops with OpenBao creds and the rendered topology.
ops() {
  "$BAO_SH" with-executor -- env BRAVO_TOPOLOGY_FILE="$STATE/topology.json" \
    "$REPO/platform/executor/.venv/bin/python" -m bravo_executor.ops "$@"
}

# executor-0 on the platform EKS is the only Executor (decision 2026-11-02 21:0x). The operator CLI
# reaches it through a private port-forward to svc/executor for this script only.
EXECUTOR_PF_PID=""
executor_url() {
  if [ -n "${EXECUTOR_URL:-}" ]; then return 0; fi
  local port
  port="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control port-forward svc/executor "$port:8081" >/dev/null 2>&1 &
  EXECUTOR_PF_PID=$!
  trap '[ -z "$EXECUTOR_PF_PID" ] || kill "$EXECUTOR_PF_PID" 2>/dev/null || true' EXIT
  EXECUTOR_URL="http://127.0.0.1:$port"
  for _ in $(seq 1 40); do curl -fsS "$EXECUTOR_URL/healthz" >/dev/null 2>&1 && return 0; sleep 0.25; done
  fail "executor-0 not reachable through port-forward ($PLATFORM_CONTEXT bravo-control/svc/executor)"
}

# Contract-test fault injection on executor-0 (test-only flag pair, audit 29). The StatefulSet is the
# platform's; only these two env vars are touched, and executor_clear_fault removes them again.
executor_set_fault() {  # executor_set_fault <resource:action>
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control set env sts/executor \
    BRAVO_ENABLE_FAULT_INJECTION=1 BRAVO_FAULT_CRASH_AFTER="$1" >/dev/null
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control rollout status sts/executor --timeout=300s >/dev/null
  EXECUTOR_URL=""; [ -z "$EXECUTOR_PF_PID" ] || kill "$EXECUTOR_PF_PID" 2>/dev/null || true; executor_url
}
executor_clear_fault() {
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control set env sts/executor \
    BRAVO_ENABLE_FAULT_INJECTION- BRAVO_FAULT_CRASH_AFTER- >/dev/null
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control rollout status sts/executor --timeout=300s >/dev/null
  EXECUTOR_URL=""; [ -z "$EXECUTOR_PF_PID" ] || kill "$EXECUTOR_PF_PID" 2>/dev/null || true; executor_url
}
executor_restarts() {
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control get pod executor-0 \
    -o jsonpath='{.status.containerStatuses[?(@.name=="executor")].restartCount}'
}
