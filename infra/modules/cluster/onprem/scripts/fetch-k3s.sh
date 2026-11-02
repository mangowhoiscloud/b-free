#!/usr/bin/env bash
# Download the K3s arm64 binary once into a host cache and verify it twice: against the release's
# sha256sum-arm64.txt and against the SHA-256 pinned in Terraform. Prints the cached path.
#   fetch-k3s.sh <version e.g. v1.36.5+k3s1> <pinned sha256>
set -euo pipefail
ver=$1 pinned=$2
cache="${BRAVO_CACHE_DIR:-$HOME/.cache/bravo}/k3s/$ver"
bin="$cache/k3s-arm64"
base="https://github.com/k3s-io/k3s/releases/download/${ver//+/%2B}"
mkdir -p "$cache"
if [[ ! -f $bin ]] || [[ $(shasum -a 256 "$bin" | cut -d' ' -f1) != "$pinned" ]]; then
  curl -fsSL --retry 3 -o "$cache/sha256sum-arm64.txt" "$base/sha256sum-arm64.txt"
  curl -fsSL --retry 3 -o "$bin.part" "$base/k3s-arm64"
  mv -f "$bin.part" "$bin"
fi
curl -fsSL --retry 3 -o "$cache/sha256sum-arm64.txt" "$base/sha256sum-arm64.txt"
release="$(awk '$2 == "k3s-arm64" {print $1}' "$cache/sha256sum-arm64.txt")"
actual="$(shasum -a 256 "$bin" | cut -d' ' -f1)"
[[ $release == "$pinned" ]] || { echo "fetch-k3s: release sha256 $release != pinned $pinned" >&2; exit 1; }
[[ $actual == "$pinned" ]] || { echo "fetch-k3s: binary sha256 $actual != pinned $pinned" >&2; exit 1; }
echo "fetch-k3s: $ver k3s-arm64 sha256 $actual (matches release sha256sum-arm64.txt and pin)" >&2
printf '%s\n' "$bin"
