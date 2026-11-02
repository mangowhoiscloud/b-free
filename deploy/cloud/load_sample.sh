#!/usr/bin/env bash
# Samples node and Pod CPU/memory from the kubelet summary API (no metrics-server needed) while a load
# runs, so that a bottleneck is measured instead of guessed. One JSON line per node per sample.
#
#   deploy/cloud/load_sample.sh <kube-context> <out.jsonl> [interval_s=5]     (stop with SIGTERM)
set -Eeuo pipefail
CTX="${1:?context}"; OUT="${2:?out}"; IV="${3:-5}"
trap 'exit 0' TERM INT
nodes="$(kubectl --context "$CTX" get nodes -o jsonpath='{.items[*].metadata.name}')"
while :; do
  for n in $nodes; do
    kubectl --context "$CTX" get --raw "/api/v1/nodes/$n/proxy/stats/summary" 2>/dev/null | jq -c --arg ctx "$CTX" '{
      ts: (now | todate), ctx: $ctx, node: .node.nodeName,
      node_cpu_m: ((.node.cpu.usageNanoCores // 0) / 1e6 | floor),
      node_mem_mib: ((.node.memory.workingSetBytes // 0) / 1048576 | floor),
      pods: ([.pods[] | {ns: .podRef.namespace, pod: .podRef.name,
                         cpu_m: ((.cpu.usageNanoCores // 0) / 1e6 | floor),
                         mem_mib: ((.memory.workingSetBytes // 0) / 1048576 | floor)}]
             | map(select(.cpu_m >= 5 or .ns == "chat" or .ns == "cf-edge")) | sort_by(-.cpu_m))}' >> "$OUT" || true
  done
  sleep "$IV" & wait $!
done
