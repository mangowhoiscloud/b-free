#!/usr/bin/env bash
# Print the node IP of a Lima VM: the address on the user-v2 network.
# With qemu (or vz plus the default network) user-v2 is a second NIC, lima0, and eth0 is the
# per-VM slirp address 192.168.5.15 (same on every VM). With vz and user-v2 as the only network
# (Lima 1.2), eth0 itself is on user-v2. Prefer lima0, otherwise the default-route source address,
# and refuse the shared slirp address.
#   node-ip.sh <instance>
set -euo pipefail
LIMACTL=${LIMACTL:-limactl}
ip="$("$LIMACTL" shell "$1" -- sh -c '
  a=$(ip -4 -o addr show dev lima0 2>/dev/null | awk "{print \$4}" | cut -d/ -f1 | head -n1)
  [ -n "$a" ] || a=$(ip -4 route show default | awk "{for(i=1;i<=NF;i++) if(\$i==\"src\") print \$(i+1)}" | head -n1)
  printf "%s" "$a"')"
[[ -n $ip && $ip != 192.168.5.15 ]] || { echo "node-ip: $1 has no user-v2 address (got '${ip}')" >&2; exit 1; }
printf '%s\n' "$ip"
