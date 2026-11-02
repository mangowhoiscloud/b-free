#!/usr/bin/env bash
# Executor ledger DB for cloud runs until the platform DB runs on the platform EKS
# (CLOUD-E2E-CONTRACT.md 6.1: no Compose pilot). A throwaway PostgreSQL container on 127.0.0.1
# with the contract tables (CONTRACT.md 5) and the Executor migrations. Every run's records are
# exported to deploy/pilot/runs/ by run_e2e.sh before the container is removed.
#   ledger.sh up | down | dsn
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
NAME=bravo-exec-ledger
case "${1:-dsn}" in
  up)
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      # Loopback-only listener with trust auth: no secret exists to leak. Removed with `down`.
      docker run -d --name "$NAME" -e POSTGRES_DB=bravo -e POSTGRES_USER=bravo_executor \
        -e POSTGRES_HOST_AUTH_METHOD=trust -p "127.0.0.1:${LEDGER_PORT}:5432" "$LEDGER_IMAGE" >/dev/null
      for _ in $(seq 1 60); do docker exec "$NAME" pg_isready -U bravo_executor -d bravo >/dev/null 2>&1 && break; sleep 1; done
      sleep 1
      for f in "$REPO/platform/executor/tests/sql/platform_contract.sql" "$REPO"/platform/executor/migrations/*.sql; do
        docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U bravo_executor -d bravo < "$f"
      done
      log "ledger up on 127.0.0.1:${LEDGER_PORT}"
    else
      log "ledger already up"
    fi
    ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 || true; log "ledger removed" ;;
  dsn) printf 'host=127.0.0.1 port=%s dbname=bravo user=bravo_executor' "$LEDGER_PORT" ;;
  *) fail "usage: ledger.sh up|down|dsn" ;;
esac
