#!/usr/bin/env bash
# Run lint, type check and tests for bravo_executor. Database tests use a throwaway
# PostgreSQL container started here and removed on exit (no mocks).
#
# Environment:
#   BRAVO_TEST_PG_IMAGE  image for the throwaway DB (default: CHAT_DB_IMAGE from deploy/pilot/.env.example)
#   BRAVO_TEST_PG_PORT   host port for it (default 35498)
set -Eeuo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$PKG_DIR/../.." && pwd)"
cd "$PKG_DIR"

if [ -z "${BRAVO_TEST_PG_IMAGE:-}" ]; then
  BRAVO_TEST_PG_IMAGE="$(sed -n 's/^CHAT_DB_IMAGE=//p' "$REPO_DIR/deploy/pilot/.env.example" | head -n 1)"
fi
: "${BRAVO_TEST_PG_IMAGE:?set BRAVO_TEST_PG_IMAGE}"
BRAVO_TEST_PG_PORT="${BRAVO_TEST_PG_PORT:-35498}"
pw="t$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
name="bravo-executor-test-$$"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" -e POSTGRES_PASSWORD="$pw" -e POSTGRES_DB=bravo_test \
  -p "127.0.0.1:${BRAVO_TEST_PG_PORT}:5432" "$BRAVO_TEST_PG_IMAGE" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d bravo_test >/dev/null 2>&1 && break
  sleep 1
done

export BRAVO_TEST_PG_IMAGE
export BRAVO_TEST_PG_DSN="host=127.0.0.1 port=${BRAVO_TEST_PG_PORT} dbname=bravo_test user=postgres password=${pw}"
uv run ruff check src tests
uv run pyright
uv run pytest -q "$@"
