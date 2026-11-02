#!/usr/bin/env bash
# Stop and delete one Lima VM and its data disk (terraform destroy).
#   node-down.sh <instance> <data-disk-name>
set -euo pipefail
name=$1 disk=$2
LIMACTL=${LIMACTL:-limactl}

if "$LIMACTL" list --json 2>/dev/null | jq -e --arg n "$name" 'select(.name == $n)' >/dev/null; then
  "$LIMACTL" stop --force "$name" 2>/dev/null || true
  "$LIMACTL" delete --force "$name"
  echo "node-down[$name]: instance deleted"
fi
if "$LIMACTL" disk list --json 2>/dev/null | jq -e --arg d "$disk" 'select(.name == $d)' >/dev/null; then
  "$LIMACTL" disk delete --force "$disk"
  echo "node-down[$name]: disk $disk deleted"
fi
