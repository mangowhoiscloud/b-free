#!/usr/bin/env bash
# Install K3s (pinned binary) on one Lima node with the CIS-oriented settings, then start it.
#   k3s-node.sh server <instance> <k3s-version> <k3s-sha256> <api-host-port>
#   k3s-node.sh agent  <instance> <k3s-version> <k3s-sha256> <server-instance>
# The join token never leaves the VMs except through a pipe between them (no file, no state).
set -euo pipefail
role=$1 name=$2 ver=$3 sha=$4 extra=$5
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
files="$here/../files"
LIMACTL=${LIMACTL:-limactl}
sh_() { "$LIMACTL" shell "$1" -- "${@:2}"; }
log() { printf 'k3s-%s[%s]: %s\n' "$role" "$name" "$*"; }
lima0_ip() { "$here/node-ip.sh" "$1"; }

bin="$("$here/fetch-k3s.sh" "$ver" "$sha")"
ip="$(lima0_ip "$name")"
[[ -n $ip ]] || { log "no user-v2 address"; exit 1; }

installed="$(sh_ "$name" sh -c '/usr/local/bin/k3s --version 2>/dev/null | head -n1' || true)"
if [[ $installed != *"$ver"* ]]; then
  log "copy k3s $ver"
  "$LIMACTL" copy "$bin" "$name:/tmp/k3s"
  sh_ "$name" sudo sh -c "echo '$sha  /tmp/k3s' | sha256sum -c - && install -m 0755 /tmp/k3s /usr/local/bin/k3s && rm -f /tmp/k3s"
  for l in kubectl crictl ctr; do sh_ "$name" sudo ln -sf /usr/local/bin/k3s "/usr/local/bin/$l"; done
fi

# Kernel defaults that kubelet --protect-kernel-defaults expects.
"$LIMACTL" copy "$files/sysctl-k3s.conf" "$name:/tmp/90-k3s.conf"
sh_ "$name" sudo sh -c 'install -m 0644 /tmp/90-k3s.conf /etc/sysctl.d/90-k3s.conf && rm -f /tmp/90-k3s.conf && sysctl --system >/dev/null'

sh_ "$name" sudo install -d -m 0700 /etc/rancher/k3s
if [[ $role == server ]]; then
  for f in psa.yaml audit-policy.yaml; do
    "$LIMACTL" copy "$files/$f" "$name:/tmp/$f"
    sh_ "$name" sudo sh -c "install -m 0600 /tmp/$f /etc/rancher/k3s/$f && rm -f /tmp/$f"
  done
  # Agent-only join secret, generated inside the server VM; agents never get the server token.
  sh_ "$name" sudo sh -c 'umask 077; test -s /etc/rancher/k3s/agent-token || head -c 32 /dev/urandom | od -An -tx1 | tr -d " \n" > /etc/rancher/k3s/agent-token'
  sh_ "$name" sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<CFG
# K3s server, bravo on-prem destination (D-21). Written by infra/modules/cluster/onprem.
node-name: $name
node-ip: $ip
advertise-address: $ip
tls-san: [127.0.0.1, localhost, $ip]
write-kubeconfig-mode: "0600"
agent-token-file: /etc/rancher/k3s/agent-token
# Cilium (VXLAN, kube-proxy replacement for the Gateway API) instead of flannel/kube-proxy/NetworkPolicy.
flannel-backend: none
disable-network-policy: true
disable-kube-proxy: true
disable: [traefik, servicelb]
cluster-cidr: 10.42.0.0/16
service-cidr: 10.43.0.0/16
# CIS-oriented settings.
protect-kernel-defaults: true
secrets-encryption: true
kube-apiserver-arg:
  - admission-control-config-file=/etc/rancher/k3s/psa.yaml
  - audit-policy-file=/etc/rancher/k3s/audit-policy.yaml
  - audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log
  - audit-log-maxage=30
  - audit-log-maxbackup=10
  - audit-log-maxsize=100
  - service-account-extend-token-expiration=false
kube-controller-manager-arg:
  - terminated-pod-gc-threshold=10
kubelet-arg:
  - streaming-connection-idle-timeout=5m
  - make-iptables-util-chains=true
  - event-qps=0
CFG
else
  server=$extra
  sip="$(lima0_ip "$server")"
  [[ -n $sip ]] || { log "server $server has no user-v2 address"; exit 1; }
  for _ in $(seq 1 60); do sh_ "$server" sudo test -s /var/lib/rancher/k3s/server/agent-token && break; sleep 2; done
  # Full-form agent token (carries the CA hash): server VM -> pipe -> agent VM, mode 600.
  sh_ "$server" sudo cat /var/lib/rancher/k3s/server/agent-token \
    | sh_ "$name" sudo sh -c 'umask 077; cat > /etc/rancher/k3s/agent-token'
  sh_ "$name" sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<CFG
# K3s agent, bravo on-prem destination (D-21). Written by infra/modules/cluster/onprem.
server: https://$sip:6443
token-file: /etc/rancher/k3s/agent-token
node-name: $name
node-ip: $ip
protect-kernel-defaults: true
kubelet-arg:
  - streaming-connection-idle-timeout=5m
  - make-iptables-util-chains=true
  - event-qps=0
CFG
fi
sh_ "$name" sudo chmod 0600 /etc/rancher/k3s/config.yaml

unit="k3s"; [[ $role == agent ]] && unit="k3s-agent"
sed "s/__ROLE__/$role/" "$files/k3s.service" | sh_ "$name" sudo tee "/etc/systemd/system/$unit.service" >/dev/null
sh_ "$name" sudo systemctl daemon-reload
sh_ "$name" sudo systemctl enable "$unit" >/dev/null 2>&1
sh_ "$name" sudo systemctl restart "$unit"

if [[ $role == server ]]; then
  for _ in $(seq 1 90); do
    sh_ "$name" sudo k3s kubectl get --raw /readyz >/dev/null 2>&1 && break
    sleep 2
  done
  sh_ "$name" sudo k3s kubectl get --raw /readyz >/dev/null || { log "API not ready"; exit 1; }
  log "API ready on $ip:6443 (laptop: 127.0.0.1:$extra)"
else
  for _ in $(seq 1 90); do
    sh_ "$extra" sudo k3s kubectl get node "$name" >/dev/null 2>&1 && break
    sleep 2
  done
  sh_ "$extra" sudo k3s kubectl get node "$name" >/dev/null || { log "agent did not register"; exit 1; }
  log "registered with server $extra"
fi
