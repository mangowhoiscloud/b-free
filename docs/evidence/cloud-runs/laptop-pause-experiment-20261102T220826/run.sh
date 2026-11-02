#!/usr/bin/env bash
# usage: run.sh <server_login_retry> <scenario rapid|single> -> JSON line
set -euo pipefail
RETRY=$1; SC=$2; T=pt$$
[ "${TLS:-0}" = 1 ] && TLSLINE="server_tls_sslmode = require" || TLSLINE=""
IMG_PG=postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a
docker network create $T >/dev/null
cleanup() { docker rm -f $T-pg $T-pgb >/dev/null 2>&1; docker network rm $T >/dev/null 2>&1; }
trap cleanup EXIT
PGARGS="-c max_connections=200"
[ "${TLS:-0}" = 1 ] && PGARGS="$PGARGS -c ssl=on -c ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem -c ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key"
[ "${SLOW:-1}" = 1 ] && PGARGS="$PGARGS -c pre_auth_delay=1"
docker run -d --name $T-pg --network $T -e POSTGRES_PASSWORD=<가림> -e POSTGRES_DB=chat $IMG_PG $PGARGS >/dev/null
for i in $(seq 1 60); do docker exec $T-pg pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; sleep 1; done; sleep 2
docker exec $T-pg psql -U postgres -d chat -qc "CREATE TABLE m (id bigserial primary key, k uuid unique, body text)"
cat > pgb.ini <<INI
[databases]
chat = host=$T-pg port=5432 dbname=chat
[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
unix_socket_dir =
auth_type = plain
auth_file = /cfg/userlist.txt
pool_mode = transaction
default_pool_size = 10
${MINPOOL:-}
max_client_conn = 200
admin_users = pgb_admin
query_wait_timeout = 8
server_login_retry = $RETRY
${TLSLINE:-}
max_prepared_statements = 0
INI
printf '"postgres" "<가림>"\n"pgb_admin" "<가림>"\n' > userlist.txt
chmod 644 pgb.ini userlist.txt
docker run -d --name $T-pgb --network $T -p 127.0.0.1::6432 -v "$PWD:/cfg:ro" -e PGBOUNCER_INI=/cfg/pgb.ini bravo-pgbouncer:pilot >/dev/null
sleep 2
PORT=$(docker port $T-pgb 6432 | head -1 | cut -d: -f2)
"<repo>/platform/executor/.venv/bin/python" load.py "$PORT" "$SC" "$RETRY"
