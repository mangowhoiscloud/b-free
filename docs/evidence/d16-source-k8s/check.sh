#!/bin/bash
# Console data paths for the source cluster: read-only GETs through the console's /svc proxy
# (kubectl -n bravo-control port-forward svc/console 18080:8080). No secret values are in these responses.
set -u
B=http://127.0.0.1:18080
S="$(dirname "$0")/summ.py"
echo "# $1  $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST')  ($(date -u +%H:%M:%SZ))"
echo "## GET /svc/collector/v1/clusters"
curl -s -m 30 "$B/svc/collector/v1/clusters" | python3 $S clusters
echo "## GET /svc/collector/v1/environments/source-aws/snapshot?sources=kubernetes"
curl -s -m 60 "$B/svc/collector/v1/environments/source-aws/snapshot?sources=kubernetes" | python3 $S snapshot
echo "## GET /svc/collector/v1/inventory"
curl -s -m 60 "$B/svc/collector/v1/inventory" | python3 $S inventory
echo "## GET /svc/gate/v1/controls/report?source=source-aws&target=target-onprem (source side, K8s questions)"
curl -s -m 60 "$B/svc/gate/v1/controls/report?source=source-aws&target=target-onprem" | python3 $S gate
