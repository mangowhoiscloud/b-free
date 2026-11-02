#!/usr/bin/env bash
# SPK-05: the pilot PgBouncer image in front of the spike RDS, as the remote PgBouncer of 12 6절 row 4:
# it verifies the provider name (verify-full, RDS CA bundle) while /etc/hosts sends that name to the link.
# docker --add-host is the same /etc/hosts mechanism as a Pod's hostAliases.
#   pgb.sh up | down
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
NAME=bravo-spike-pgb
case ${1:?usage} in
  up)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -p 127.0.0.1:46432:6432 \
      --add-host "$RDS_HOST:host-gateway" --add-host "wrong-name.example:host-gateway" \
      --env-file <(printf 'APP_WRITER_PASSWORD=%s\nPGB_ADMIN_PASSWORD=%s\n' \
          "$("$SPIKE_HERE/bao.sh" get envs/spike-db/rds app_writer)" "$("$SPIKE_HERE/bao.sh" get envs/spike-db/pgbouncer pgb_admin)") \
      -v "$SPIKE_TLS/rds-ap-northeast-2-bundle.pem:/tls/rds-ca.pem:ro" --tmpfs /run/pgb:mode=0777 \
      --user 0:0 --entrypoint sh bravo-pgbouncer:pilot -c '
        umask 077
        printf "\"app_writer\" \"%s\"\n\"pgb_admin\" \"%s\"\n" "$APP_WRITER_PASSWORD" "$PGB_ADMIN_PASSWORD" > /run/pgb/userlist.txt
        unset APP_WRITER_PASSWORD PGB_ADMIN_PASSWORD
        cat > /run/pgb/pgbouncer.ini <<INI
[databases]
chat = host='"$RDS_HOST"' port='"$RDS_LOCAL_PORT"' dbname=chat
chat_wrongname = host=wrong-name.example port='"$RDS_LOCAL_PORT"' dbname=chat
[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
unix_socket_dir =
auth_type = scram-sha-256
auth_file = /run/pgb/userlist.txt
pool_mode = transaction
default_pool_size = 5
max_prepared_statements = 0
admin_users = pgb_admin
query_wait_timeout = 8
server_connect_timeout = 5
server_login_retry = 1
server_tls_sslmode = verify-full
server_tls_ca_file = /tls/rds-ca.pem
ignore_startup_parameters = extra_float_digits,options
log_pooler_errors = 1
INI
        chown -R 10070:10070 /run/pgb
        exec su -s /bin/sh bravo-pgb -c "exec pgbouncer /run/pgb/pgbouncer.ini"' >/dev/null
    sleep 2; docker logs "$NAME" 2>&1 | tail -3
    docker exec "$NAME" getent hosts "$RDS_HOST" wrong-name.example
    ;;
  down) docker rm -f "$NAME" ;;
esac
