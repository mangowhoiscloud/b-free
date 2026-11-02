#!/bin/sh
# Build the agent sandbox images for the native platform.
# Usage: images/build.sh [name ...]   (default: all five; ci-agent last because it needs templates/bravo-app)
set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ctx=$(CDPATH='' cd -- "$here/.." && pwd)   # build context = platform/agent
ALL="discovery-agent governance-agent app-migration-agent db-agent ci-agent"
names=${*:-$ALL}

cd "$ctx"
for n in $names; do
  if [ ! -f "images/$n/Dockerfile" ]; then
    echo "build.sh: unknown image '$n' (expected one of: $ALL)" >&2
    exit 2
  fi
  echo "==> building bravo/agent-sandbox-$n:dev"
  docker build -f "images/$n/Dockerfile" -t "bravo/agent-sandbox-$n:dev" "$ctx"
done
