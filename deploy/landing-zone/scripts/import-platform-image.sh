#!/usr/bin/env bash
# Put one digest-pinned platform image (ECR) onto every on-prem RKE2 node, under its own <repo>@<digest> name, for the
# rehearsal verification Jobs (platform/rehearsal behavioral.py runs the gate's OBJ-01..06 on the GATE image, pinned
# BRAVO_REHEARSAL_GATE_PROBE_IMAGE). The nodes have no ECR credential; nothing is pulled there.
#   import-platform-image.sh <ecr-repo>@sha256:<digest> [lima-node ...]
# skopeo copies the OCI index with its digests preserved (no conversion), ctr imports it for linux/arm64 and tags
# <repo>@<digest>. The archive's own tag is KEPT: containerd's CRI image store keeps the first name it saw for an image
# and fails the container start if that name is removed ("failed to get image ... <name>: not found", measured
# 2026-11-03 03:02 KST on bravo-onprem-agent). Re-run after a gate pin change. ECR login: the operator's AWS profile.
set -euo pipefail
ref=${1:?usage: import-platform-image.sh <repo>@sha256:<digest> [node ...]}; shift
[[ $ref == *@sha256:* ]] || { echo "import-platform-image: need a digest reference" >&2; exit 2; }
nodes=("$@"); ((${#nodes[@]})) || nodes=(bravo-onprem-server bravo-onprem-agent)
repo=${ref%@*} digest=${ref#*@} registry=${ref%%/*}
# Fully qualified: containerd's CRI records the normalized name (docker.io/...); ctr keeps the archive's name as is, so
# an unqualified tag left CRI pointing at a name ctr did not have (measured 2026-11-03 03:37 KST, same 400 as below).
local_tag="docker.io/bravo-import/$(basename "$repo"):${digest:7:12}"
ctr="sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock -n k8s.io"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
aws ecr get-login-password --region ap-northeast-2 | skopeo login --username AWS --password-stdin "$registry" >/dev/null
skopeo copy --quiet --all --preserve-digests "docker://$ref" "oci-archive:$work/image.tar:$local_tag"
for n in "${nodes[@]}"; do
  limactl shell "$n" -- sh -c 'cat > /tmp/bravo-import.tar' <"$work/image.tar"
  limactl shell "$n" -- sh -c "$ctr images import --platform linux/arm64 /tmp/bravo-import.tar >/dev/null \
    && $ctr images tag --force $local_tag '$ref' >/dev/null; rm -f /tmp/bravo-import.tar" </dev/null
  got="$(limactl shell "$n" -- sh -c "$ctr images ls 'name==$ref'" </dev/null | awk 'NR==2 {print $3}')"
  [[ $got == "$digest" ]] || { echo "import-platform-image: $n has $got, expected $digest" >&2; exit 1; }
  echo "import-platform-image: $n $ref"
done
