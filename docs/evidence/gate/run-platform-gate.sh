#!/usr/bin/env bash
# Run the gate for both pairs through the PLATFORM gate service the way the platform API evaluates a plan
# (POST http://gate.bravo-control.svc:8084/v1/controls/report {"source", "target"}) and save raw reports + 통제 ID별 비교표.
#   run-platform-gate.sh [pod/<name> | svc/gate]        default svc/gate
# The POST run does the write probes (OBJ-03/OBJ-04) on the gate's own _gate/ keys and deletes them again.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
TARGET="${1:-svc/gate}"
OUT="$HERE/platform-gate-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
port="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
kubectl --context "$CTX" -n bravo-control port-forward "$TARGET" "$port:8084" >/dev/null 2>&1 &
pf=$!; trap 'kill $pf 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:$port/healthz" >/dev/null && break; sleep 0.25; done
{
  echo "target: $TARGET (context $CTX, ns bravo-control)"
  kubectl --context "$CTX" -n bravo-control get deploy gate -o jsonpath='deployment image: {.spec.template.spec.containers[0].image}{"\n"}'
  [[ $TARGET == pod/* ]] && kubectl --context "$CTX" -n bravo-control get "$TARGET" -o jsonpath='pod image: {.spec.containers[0].image}{"\n"}'
  kubectl --context "$CTX" -n argocd get app control -o jsonpath='argocd control: {.status.sync.revision} {.status.sync.status}{"\n"}'
  curl -s "http://127.0.0.1:$port/v1/environments"; echo
} >"$OUT/run.txt"
for t in target-onprem target-gcp; do
  start="$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST')"
  curl -s -m 600 -X POST -H 'content-type: application/json' -d "{\"source\":\"source-aws\",\"target\":\"$t\"}" \
    "http://127.0.0.1:$port/v1/controls/report" >"$OUT/report-source-aws-$t.json"
  echo "$t: started $start, finished $(TZ=Asia/Seoul date '+%H:%M:%S KST')" >>"$OUT/run.txt"
done
python3 -I "$HERE/gate-table.py" "$OUT" >"$OUT/TABLES.md"
echo "$OUT"
