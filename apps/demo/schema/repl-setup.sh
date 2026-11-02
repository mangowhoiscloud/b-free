#!/usr/bin/env bash
# One-shot replication setup (compose service `repl-setup`). Runs after src-db and
# dst-db are healthy. Idempotent: an existing subscription is left as it is.
#
#   dst: bravo_fwd_sub  -> src publication bravo_fwd (initial copy, origin = none)
#   src: bravo_rev_sub  -> dst publication bravo_rev (enabled = false, copy_data = false,
#                          origin = none, create_slot = false, slot pre-created on dst)
# Both subscriptions are then owned by executor_ctl so the Executor can ENABLE/DISABLE
# them without superuser rights.
#
# Required environment: POSTGRES_PASSWORD, REPLICATOR_PASSWORD, SRC_HOST, DST_HOST, DB_PORT,
#                       CHAT_DB, SYNC_TIMEOUT_S
#
# Secrets (D-26): no password is in a command-line argument. The superuser password reaches psql
# through PGPASSWORD; psql builds the subscription conninfo itself from REPLICATOR_PASSWORD with
# \getenv. PostgreSQL 16 requires the password inside the conninfo when the subscription owner
# is not a superuser (executor_ctl owns both subscriptions; `passfile=` is rejected with "Non-
# superusers must provide a password in the connection string"), so CREATE SUBSCRIPTION carries
# it once. That session sets log_statement = none and log_min_error_statement = panic, so the
# statement is never written to the server log, and the stored conninfo (pg_subscription.
# subconninfo) is readable by superusers only.
set -Eeuo pipefail

: "${POSTGRES_PASSWORD:?repl-setup: POSTGRES_PASSWORD is required}"
: "${REPLICATOR_PASSWORD:?repl-setup: REPLICATOR_PASSWORD is required}"
: "${SRC_HOST:?repl-setup: SRC_HOST is required}"
: "${DST_HOST:?repl-setup: DST_HOST is required}"
: "${DB_PORT:?repl-setup: DB_PORT is required}"
: "${CHAT_DB:?repl-setup: CHAT_DB is required}"
: "${SYNC_TIMEOUT_S:?repl-setup: SYNC_TIMEOUT_S is required}"
export PGPASSWORD="$POSTGRES_PASSWORD"

psql_on() {
  local host="$1"; shift
  psql -v ON_ERROR_STOP=1 --no-psqlrc -h "$host" -p "$DB_PORT" -U postgres -d "$CHAT_DB" "$@"
}

subscription_exists() {
  local host="$1" name="$2"
  [ "$(psql_on "$host" -tAc "SELECT count(*) FROM pg_subscription WHERE subname = '${name}'")" = "1" ]
}

export REPLICATOR_PASSWORD DB_PORT CHAT_DB

# Runs on the subscriber. Reads REPLICATOR_PASSWORD, DB_PORT and CHAT_DB with \getenv (not argv)
# and sets :conninfo for the SQL that follows on stdin. Publisher host name: psql -v publisher=...
QUIET_CONNINFO="$(cat <<'PSQL'
SET log_statement = none;
SET log_min_error_statement = panic;
\getenv repl_password REPLICATOR_PASSWORD
\getenv db_port DB_PORT
\getenv chat_db CHAT_DB
\set conninfo 'host=' :publisher ' port=' :db_port ' dbname=' :chat_db ' user=replicator password=' :repl_password
PSQL
)"

if subscription_exists "$DST_HOST" bravo_fwd_sub; then
  echo "repl-setup: bravo_fwd_sub already exists on ${DST_HOST}"
else
  echo "repl-setup: creating bravo_fwd_sub on ${DST_HOST}"
  { printf '%s\n' "$QUIET_CONNINFO"; cat <<'SQL'; } | psql_on "$DST_HOST" -v publisher="$SRC_HOST"
CREATE SUBSCRIPTION bravo_fwd_sub
  CONNECTION :'conninfo'
  PUBLICATION bravo_fwd
  WITH (origin = none);
ALTER SUBSCRIPTION bravo_fwd_sub OWNER TO executor_ctl;
SQL
fi

if subscription_exists "$SRC_HOST" bravo_rev_sub; then
  echo "repl-setup: bravo_rev_sub already exists on ${SRC_HOST}"
else
  echo "repl-setup: creating disabled bravo_rev_sub on ${SRC_HOST}"
  { printf '%s\n' "$QUIET_CONNINFO"; cat <<'SQL'; } | psql_on "$SRC_HOST" -v publisher="$DST_HOST"
CREATE SUBSCRIPTION bravo_rev_sub
  CONNECTION :'conninfo'
  PUBLICATION bravo_rev
  WITH (enabled = false, copy_data = false, origin = none,
        create_slot = false, slot_name = 'bravo_rev_sub');
ALTER SUBSCRIPTION bravo_rev_sub OWNER TO executor_ctl;
SQL
fi

echo "repl-setup: waiting for initial table sync on ${DST_HOST} (timeout ${SYNC_TIMEOUT_S}s)"
deadline=$(( $(date +%s) + SYNC_TIMEOUT_S ))
while :; do
  pending="$(psql_on "$DST_HOST" -tAc \
    "SELECT count(*) FROM pg_subscription_rel r JOIN pg_subscription s ON s.oid = r.srsubid
      WHERE s.subname = 'bravo_fwd_sub' AND r.srsubstate <> 'r'")"
  streaming="$(psql_on "$DST_HOST" -tAc \
    "SELECT count(*) FROM pg_stat_subscription WHERE subname = 'bravo_fwd_sub' AND relid IS NULL AND pid IS NOT NULL")"
  if [ "$pending" = "0" ] && [ "$streaming" = "1" ]; then
    echo "repl-setup: forward subscription is streaming and all tables are ready"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "repl-setup: timeout (pending=${pending} streaming=${streaming})" >&2
    exit 1
  fi
  sleep 1
done
