#!/usr/bin/env bash
# Laptop plain PostgreSQL 16.15 (same image as the pilot) as the self-run side (C) of the RDS spikes.
#   spike_pg.sh up TLS_DIR    start bravo-spike-pg on 127.0.0.1:45432, TLS on, hostnossl reject, schema as dst
#   spike_pg.sh down          remove the container and its volume
# TLS_DIR holds ca.crt, server.crt, server.key, pg_hba.conf (spike-only private CA, scratchpad).
# Passwords come from OpenBao secret/envs/spike-db/pilot-pg through pipes only (D-26).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
NAME=bravo-spike-pg
IMAGE=postgres:16.15
BAO="$HERE/bao.sh"
SECRET=envs/spike-db/pilot-pg

case ${1:?usage} in
  up)
    tls=${2:?TLS_DIR}
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -p 127.0.0.1:45432:5432 \
      --env-file <(printf 'POSTGRES_PASSWORD=%s\nPOSTGRES_DB=chat\n' "$("$BAO" get $SECRET postgres)") \
      -v "$tls:/tls:ro" -v "$REPO/apps/demo/schema/sql:/schema:ro" \
      --entrypoint bash "$IMAGE" -c '
        install -o postgres -g postgres -m 600 /tls/server.key /etc/server.key
        install -o postgres -g postgres -m 644 /tls/server.crt /etc/server.crt
        install -o postgres -g postgres -m 644 /tls/pg_hba.conf /etc/pg_hba.conf
        exec docker-entrypoint.sh postgres -c wal_level=logical -c max_replication_slots=10 \
          -c max_wal_senders=10 -c max_slot_wal_keep_size=2048 -c ssl=on \
          -c ssl_cert_file=/etc/server.crt -c ssl_key_file=/etc/server.key -c hba_file=/etc/pg_hba.conf \
          -c password_encryption=scram-sha-256 -c log_replication_commands=on -c cluster_name=spike-pg \
          -c log_min_error_statement=panic' >/dev/null
    for _ in $(seq 60); do
      docker exec "$NAME" pg_isready -U postgres -d chat -h 127.0.0.1 >/dev/null 2>&1 && break; sleep 1
    done
    sleep 2
    psqlc() { docker exec -i "$NAME" psql -v ON_ERROR_STOP=1 --no-psqlrc -U postgres -d chat "$@"; }
    psqlc -f /schema/01_schema.sql >/dev/null
    { printf '\\set app_writer_password %s\n' "'$("$BAO" get $SECRET app_writer)'"
      printf '\\set replicator_password %s\n' "'$("$BAO" get $SECRET replicator)'"
      printf '\\set executor_ctl_password %s\n' "'$("$BAO" get $SECRET executor_ctl)'"
      printf '\\set executor_observe_password %s\n' "'$("$BAO" get $SECRET executor_observe)'"
      cat "$REPO/apps/demo/schema/sql/02_roles.sql"; } | psqlc -f - >/dev/null
    { printf '\\set attachment_store_id %s\n' "'dst'"; cat "$REPO/apps/demo/schema/sql/03_dst.sql"; } | psqlc -f - >/dev/null
    psqlc -tAc "select 'spike-pg ready: ' || version()"
    ;;
  down) docker rm -f -v "$NAME" ;;
  *) echo "usage: spike_pg.sh up TLS_DIR | down" >&2; exit 2 ;;
esac
