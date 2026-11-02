#!/usr/bin/env bash
# 파일럿 데이터면(출발지 · 목적지 DB, PgBouncer, Executor)을 처음 상태로 되돌립니다.
# 플랫폼 DB(계획 · 감사 기록)와 관측 데이터는 남깁니다. 심은 퇴행(목적지 PUBLIC 읽기)도 다시 생깁니다.
set -euo pipefail
PILOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${PILOT_COMPOSE_PROJECT:-bravo-pilot}"
c() { docker compose -p "$PROJECT" --env-file "$PILOT/.env" -f "$PILOT/compose.yaml" "$@"; }
c rm -sf src-db dst-db pgbouncer pgbouncer-config repl-setup executor
for v in src-data dst-data pgbouncer-config; do docker volume rm "${PROJECT}_${v}" >/dev/null 2>&1 || true; done
c up -d
for _ in $(seq 1 60); do
  if curl -fsS "${SMOKE_EXECUTOR_URL:-http://localhost:8081}/v1/readiness" 2>/dev/null | grep -q '"sub_state":"streaming"'; then
    echo "데이터면 준비됨(정방향 복제 streaming)"; exit 0
  fi
  sleep 2
done
echo "데이터면이 120초 안에 준비되지 않았습니다" >&2; exit 1
