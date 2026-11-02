#!/bin/sh
# One-shot init for the shared `pgbouncer-config` volume (compose service `pgbouncer-config`).
#
# - pgbouncer.ini: copied from the repository only when the volume has none, so a
#   target written by the Executor survives `compose up` (CONTRACT.md 4).
# - userlist.txt: rendered from the environment on every run. Plain-text entries are
#   valid with auth_type = scram-sha-256: PgBouncer runs SCRAM with the client and with
#   PostgreSQL 16 (password_encryption = scram-sha-256). Format: userlist.txt.example.
#   Passwords must not contain a double quote.
#
# Ownership: PGB_UID/PGB_GID is the non-root `bravo-pgb` user of pgbouncer/Dockerfile. The
# Executor container joins PGB_GID (group_add) and rewrites the ini through the group bit.
set -eu

: "${APP_WRITER_PASSWORD:?APP_WRITER_PASSWORD is required}"
: "${PGB_ADMIN_PASSWORD:?PGB_ADMIN_PASSWORD is required}"
: "${PGB_STATS_PASSWORD:?PGB_STATS_PASSWORD is required}"

: "${CONF_DIR:?CONF_DIR is required}"
: "${SEED_INI:?SEED_INI is required}"
: "${PGB_UID:?PGB_UID is required}"
: "${PGB_GID:?PGB_GID is required}"

for v in "$APP_WRITER_PASSWORD" "$PGB_ADMIN_PASSWORD" "$PGB_STATS_PASSWORD"; do
  case "$v" in *'"'*) echo "render-config: passwords must not contain '\"'" >&2; exit 1 ;; esac
done

if [ -f "${CONF_DIR}/pgbouncer.ini" ]; then
  echo "render-config: keeping existing ${CONF_DIR}/pgbouncer.ini"
else
  cp "$SEED_INI" "${CONF_DIR}/pgbouncer.ini"
  echo "render-config: seeded ${CONF_DIR}/pgbouncer.ini"
fi

umask 077
tmp="${CONF_DIR}/.userlist.txt.$$"
printf '"%s" "%s"\n' \
  app_writer "$APP_WRITER_PASSWORD" \
  pgb_admin "$PGB_ADMIN_PASSWORD" \
  pgb_stats "$PGB_STATS_PASSWORD" > "$tmp"
mv "$tmp" "${CONF_DIR}/userlist.txt"

chown "${PGB_UID}:${PGB_GID}" "$CONF_DIR" "${CONF_DIR}/pgbouncer.ini" "${CONF_DIR}/userlist.txt"
chmod 0775 "$CONF_DIR"
chmod 0664 "${CONF_DIR}/pgbouncer.ini"
chmod 0640 "${CONF_DIR}/userlist.txt"
echo "render-config: userlist.txt rendered (3 users)"
