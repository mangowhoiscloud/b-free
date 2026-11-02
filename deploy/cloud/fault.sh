#!/usr/bin/env bash
# 장애 주입 (spec S4 "목적지 앱 오류 응답"): set the demo app's injected HTTP 5xx rate on every app pod of
# one customer cluster (apps/demo admin PUT /admin/fault). Each pod calls its own admin port from inside
# (kubectl exec), with the admin token file the pod already has: the token never leaves the pod.
# /healthz and /readyz keep answering, so the pods stay in the Service and in the Cloudflare pool.
#   fault.sh <src|dst> <rate 0..1>      e.g. fault.sh dst 0.3 ; fault.sh dst 0 clears it
# Prints one JSON line per pod. Customer clusters only (never the platform).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
side="${1:?usage: fault.sh <src|dst> <rate>}"; rate="${2:?usage: fault.sh <src|dst> <rate>}"
case "$side" in src) ctx="$SRC_CONTEXT" ;; dst) ctx="$DST_CONTEXT" ;; *) fail "side must be src or dst" ;; esac
python3 -c 'import sys; r = float(sys.argv[1]); sys.exit(0 if 0 <= r <= 1 else 1)' "$rate" || fail "rate must be 0..1"
pods="$(kubectl --context "$ctx" -n "$CHAT_NAMESPACE" get pods -l app.kubernetes.io/name=bravo-demo \
  --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}')"
[ -n "$pods" ] || fail "no running demo pods in $ctx"
for pod in $pods; do
  kubectl --context "$ctx" -n "$CHAT_NAMESPACE" exec "$pod" -c app -- python -c '
import json, os, sys, urllib.request
tok = open(os.environ["ADMIN_TOKEN_FILE"], encoding="utf-8").read().strip()
req = urllib.request.Request("http://127.0.0.1:%s/admin/fault" % os.environ.get("ADMIN_PORT", "8081"),
    data=json.dumps({"http_5xx_rate": float(sys.argv[1])}).encode(), method="PUT",
    headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=10).read().decode())' "$rate" \
    | jq -c --arg pod "$pod" --arg ctx "$ctx" --arg at "$(date -u +%FT%TZ)" \
        '{label: "장애 주입", context: $ctx, pod: $pod, at: $at} + .'
done
