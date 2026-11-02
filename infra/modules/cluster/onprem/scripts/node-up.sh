#!/usr/bin/env bash
# Create and start one Lima VM for a K3s node (idempotent). Called by Terraform (terraform_data).
#   node-up.sh <instance> <lima-yaml> <data-disk-name> <data-disk-size>
set -euo pipefail
name=$1 yaml=$2 disk=$3 disk_size=$4
LIMACTL=${LIMACTL:-limactl}

log() { printf 'node-up[%s]: %s\n' "$name" "$*"; }

if ! "$LIMACTL" disk list --json 2>/dev/null | jq -e --arg d "$disk" 'select(.name == $d)' >/dev/null; then
  log "create raw data disk $disk ($disk_size)"
  "$LIMACTL" disk create "$disk" --size "$disk_size" --format raw
fi

status="$("$LIMACTL" list --json 2>/dev/null | jq -r --arg n "$name" 'select(.name == $n) | .status')"
if [[ -z $status ]]; then
  # Two nodes are created in parallel; the first limactl run also writes ~/.lima/_config. Serialise
  # create with a lock directory so they do not race on that.
  lock="${TMPDIR:-/tmp}/bravo-lima-create.lock"
  until mkdir "$lock" 2>/dev/null; do sleep 1; done
  trap 'rmdir "$lock" 2>/dev/null || true' EXIT
  log "create instance from $yaml"
  "$LIMACTL" create --tty=false --name "$name" "$yaml"
  rmdir "$lock"; trap - EXIT
  status=Stopped
fi
if [[ $status != Running ]]; then
  log "start (status was $status)"
  "$LIMACTL" start --tty=false --timeout 15m "$name"
fi

# The node IP is the VM's address on the user-v2 network (scripts/node-ip.sh).
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _ in $(seq 1 60); do
  ip="$("$here/node-ip.sh" "$name" 2>/dev/null || true)"
  [[ -n $ip ]] && break
  sleep 2
done
[[ -n ${ip-} ]] || { log "no address on the user-v2 network"; exit 1; }
log "running, node IP (user-v2) $ip"
