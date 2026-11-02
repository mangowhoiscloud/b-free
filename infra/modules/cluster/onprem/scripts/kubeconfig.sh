#!/usr/bin/env bash
# Merge the K3s admin kubeconfig into ~/.kube/config as context <ctx> (server via the Lima
# port forward on 127.0.0.1). The current context is left as it was.
#   kubeconfig.sh add <server-instance> <ctx> <host-port>
#   kubeconfig.sh remove <ctx>
set -euo pipefail
LIMACTL=${LIMACTL:-limactl}
kc="${KUBECONFIG_TARGET:-$HOME/.kube/config}"
mkdir -p "$(dirname "$kc")"; touch "$kc"; chmod 600 "$kc"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT; chmod 700 "$work"

if [[ $1 == remove ]]; then
  ctx=$2
  kubectl --kubeconfig "$kc" config delete-context "$ctx" >/dev/null 2>&1 || true
  kubectl --kubeconfig "$kc" config delete-cluster "$ctx" >/dev/null 2>&1 || true
  kubectl --kubeconfig "$kc" config delete-user "$ctx" >/dev/null 2>&1 || true
  echo "kubeconfig: context $ctx removed"
  exit 0
fi

server=$2 ctx=$3 port=$4
"$LIMACTL" shell "$server" -- sudo cat "${KUBECONFIG_PATH_IN_VM:-/etc/rancher/rke2/rke2.yaml}" \
  | sed -e "s#https://127.0.0.1:6443#https://127.0.0.1:$port#" -e "s/: default$/: $ctx/" -e "s/^\(\s*name:\) default$/\1 $ctx/" \
  > "$work/new"
yq -i ".clusters[0].name = \"$ctx\" | .users[0].name = \"$ctx\" | .contexts[0].name = \"$ctx\" |
  .contexts[0].context.cluster = \"$ctx\" | .contexts[0].context.user = \"$ctx\" | del(.\"current-context\")" "$work/new"
cur="$(kubectl --kubeconfig "$kc" config current-context 2>/dev/null || true)"
kubectl --kubeconfig "$kc" config delete-context "$ctx" >/dev/null 2>&1 || true
kubectl --kubeconfig "$kc" config delete-cluster "$ctx" >/dev/null 2>&1 || true
kubectl --kubeconfig "$kc" config delete-user "$ctx" >/dev/null 2>&1 || true
KUBECONFIG="$kc:$work/new" kubectl config view --flatten > "$work/merged"
[[ -n $cur ]] && kubectl --kubeconfig "$work/merged" config use-context "$cur" >/dev/null
install -m 600 "$work/merged" "$kc.bravo-tmp" && mv -f "$kc.bravo-tmp" "$kc"
kubectl --context "$ctx" get --raw /readyz >/dev/null && echo "kubeconfig: context $ctx -> https://127.0.0.1:$port (current context unchanged: ${cur:-none})"
