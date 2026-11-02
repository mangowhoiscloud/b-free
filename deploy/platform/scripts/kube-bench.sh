#!/usr/bin/env bash
# Run kube-bench rke2-cis-1.9 on the platform RKE2 node, save the JSON under docs/evidence/kube-bench/, delete the namespace.
set -euo pipefail
CTX="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/../../../docs/evidence/kube-bench"; mkdir -p "$OUT"
f="$OUT/platform-rke2-$(date -u +%Y%m%dT%H%M%SZ).json"
kubectl --context "$CTX" delete ns kube-bench --ignore-not-found --wait=true >/dev/null
kubectl --context "$CTX" apply -f "$HERE/../kube-bench/job-rke2.yaml" >/dev/null
kubectl --context "$CTX" -n kube-bench wait --for=condition=complete job/kube-bench-rke2 --timeout=300s >/dev/null || \
  kubectl --context "$CTX" -n kube-bench wait --for=condition=failed job/kube-bench-rke2 --timeout=10s >/dev/null || true
kubectl --context "$CTX" -n kube-bench logs job/kube-bench-rke2 | sed -n "/^{/,\$p" > "$f"
kubectl --context "$CTX" delete ns kube-bench --wait=false >/dev/null
jq -r '"benchmark rke2-cis-1.9 (approx. for RKE2 v1.36): pass=\(.Totals.total_pass) fail=\(.Totals.total_fail) warn=\(.Totals.total_warn) info=\(.Totals.total_info)"' "$f"
jq -r '.Controls[] | .tests[] | .results[] | select(.status=="FAIL") | "FAIL \(.test_number) \(.test_desc)"' "$f" | head -40
echo "saved $f"
