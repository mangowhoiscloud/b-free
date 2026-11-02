#!/usr/bin/env bash
# Register the environments once the operator has signed in through Access (platform/api admin.py register-envs).
set -euo pipefail
CTX="${BRAVO_PLATFORM_CONTEXT:-bravo-platform-rke2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
n="$(kubectl --context "$CTX" -n bravo-control exec platform-db-1 -c postgres -- psql -d bravo -Atc \
  "select count(*) from memberships m join principals p on p.id = m.principal_id where p.email = 'ryoo0504@naver.com' and m.role = 'operator'")"
[[ $n -ge 1 ]] || { echo "register-envs: the operator ryoo0504@naver.com has not signed in through Access yet (no operator membership); not run"; exit 1; }
kubectl --context "$CTX" -n bravo-control delete job register-envs --ignore-not-found >/dev/null
kubectl kustomize --load-restrictor LoadRestrictionsNone "$HERE/../register-envs" | kubectl --context "$CTX" apply -f - >/dev/null
kubectl --context "$CTX" -n bravo-control wait --for=condition=complete job/register-envs --timeout=180s >/dev/null || true
kubectl --context "$CTX" -n bravo-control logs job/register-envs
