#!/usr/bin/env bash
# Init entry for the PostgreSQL 16 Docker Official Image (/docker-entrypoint-initdb.d). Runs once, on an
# empty data directory, against the temporary server (unix socket only).
#
# Required environment:
#   BRAVO_SIDE              src | dst
#   BRAVO_SCHEMA_DIR        directory holding 01_schema.sql, 02_roles.sql, 03_<side>.sql
#   APP_WRITER_PASSWORD, REPLICATOR_PASSWORD, EXECUTOR_CTL_PASSWORD, EXECUTOR_OBSERVE_PASSWORD
# The file is executable, so the image entrypoint runs it in its own shell (not sourced).
set -Eeuo pipefail

bravo_schema_dir="${BRAVO_SCHEMA_DIR:?bravo-init: BRAVO_SCHEMA_DIR is required}"

bravo_psql() {
  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"
}

bravo_init() {
  case "${BRAVO_SIDE:-}" in
    src|dst) ;;
    *) echo "bravo-init: BRAVO_SIDE must be src or dst" >&2; return 1 ;;
  esac
  : "${APP_WRITER_PASSWORD:?bravo-init: APP_WRITER_PASSWORD is required}"
  : "${REPLICATOR_PASSWORD:?bravo-init: REPLICATOR_PASSWORD is required}"
  : "${EXECUTOR_CTL_PASSWORD:?bravo-init: EXECUTOR_CTL_PASSWORD is required}"
  : "${EXECUTOR_OBSERVE_PASSWORD:?bravo-init: EXECUTOR_OBSERVE_PASSWORD is required}"

  echo "bravo-init: side=${BRAVO_SIDE}"
  bravo_psql -f "${bravo_schema_dir}/01_schema.sql"
  bravo_psql \
    -v app_writer_password="$APP_WRITER_PASSWORD" \
    -v replicator_password="$REPLICATOR_PASSWORD" \
    -v executor_ctl_password="$EXECUTOR_CTL_PASSWORD" \
    -v executor_observe_password="$EXECUTOR_OBSERVE_PASSWORD" \
    -f "${bravo_schema_dir}/02_roles.sql"
  bravo_psql \
    -v attachment_store_id="${BRAVO_ATTACHMENT_STORE_ID:-$BRAVO_SIDE}" \
    -f "${bravo_schema_dir}/03_${BRAVO_SIDE}.sql"
  echo "bravo-init: done"
}

bravo_init
