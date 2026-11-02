#!/usr/bin/env bash
# Apply the common RKE2 cloud-init (cloud-init/rke2-node.yaml.tftpl) to one Lima VM and join it.
# Lima cannot take extra user-data, so the same #cloud-config document is applied inside the VM with
# cloud-init itself: `cloud-init single --name write_files` writes the files exactly as at first
# boot, then the document's runcmd entry (/usr/local/sbin/bravo-rke2-bootstrap.sh) is run.
#   rke2-node.sh server <instance> <cloud-config file> <api-host-port>
#   rke2-node.sh agent  <instance> <cloud-config file> <server-instance>
# The agent join token moves server VM -> pipe -> agent VM only (no file on the laptop, no state).
set -euo pipefail
role=$1 name=$2 doc=$3 extra=$4
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIMACTL=${LIMACTL:-limactl}
sh_() { "$LIMACTL" shell "$1" -- "${@:2}" </dev/null; }
log() { printf 'rke2-%s[%s]: %s\n' "$role" "$name" "$*"; }

"$LIMACTL" copy "$doc" "$name:/tmp/bravo-cloud-config.yaml"
sh_ "$name" sudo sh -c 'install -m 0600 /tmp/bravo-cloud-config.yaml /etc/bravo-cloud-config.yaml && rm -f /tmp/bravo-cloud-config.yaml'
sh_ "$name" sudo cloud-init schema --config-file /etc/bravo-cloud-config.yaml
sh_ "$name" sudo cloud-init single --file /etc/bravo-cloud-config.yaml --name write_files --frequency always >/dev/null

if [[ $role == agent ]]; then
  server=$extra
  sip="$("$here/node-ip.sh" "$server")"
  for _ in $(seq 1 90); do sh_ "$server" sudo test -s /var/lib/rancher/rke2/server/agent-token && break; sleep 2; done
  sh_ "$name" sudo install -d -m 0700 /etc/rancher/rke2/config.yaml.d
  { printf 'server: https://%s:9345\ntoken: ' "$sip"
    "$LIMACTL" shell "$server" -- sudo cat /var/lib/rancher/rke2/server/agent-token </dev/null; printf '\n'; } \
    | "$LIMACTL" shell "$name" -- sudo sh -c 'umask 077; cat > /etc/rancher/rke2/config.yaml.d/20-join.yaml'
fi

# runcmd of the document.
sh_ "$name" sudo /usr/local/sbin/bravo-rke2-bootstrap.sh

if [[ $role == server ]]; then
  for _ in $(seq 1 150); do
    sh_ "$name" sudo test -s /etc/rancher/rke2/rke2.yaml && \
      sh_ "$name" sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml get --raw /readyz >/dev/null 2>&1 && break
    sleep 4
  done
  sh_ "$name" sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml get --raw /readyz >/dev/null \
    || { log "API not ready"; sh_ "$name" sudo journalctl -u rke2-server --no-pager -n 30; exit 1; }
  log "API ready (laptop: https://127.0.0.1:$extra)"
else
  for _ in $(seq 1 150); do
    sh_ "$extra" sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml get node "$name" >/dev/null 2>&1 && break
    sleep 4
  done
  sh_ "$extra" sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml get node "$name" >/dev/null \
    || { log "agent did not register"; sh_ "$name" sudo journalctl -u rke2-agent --no-pager -n 30; exit 1; }
  log "registered with server $extra"
fi
