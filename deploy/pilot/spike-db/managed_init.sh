#!/usr/bin/env bash
# Runs INSIDE a postgres:16.15 container (psql 16 for \getenv). Applies the chat schema to a managed DB
# the way apps/demo/schema/00-bravo-init.sh does (SCRAM verifiers hashed here, never a password in SQL text).
# libpq env (PGHOST, PGHOSTADDR, PGPORT, PGSSLMODE, PGSSLROOTCERT, PGUSER, PGPASSWORD, PGDATABASE) points at it.
#   managed_init.sh dryrun   01 + stock 02 + 03_src in one transaction, ON_ERROR_ROLLBACK, then ROLLBACK
#   managed_init.sh apply    01 + /variant/02_roles.sql + 03_src, ON_ERROR_STOP
# Passwords arrive on stdin, one "NAME=value" per line (APP_WRITER_PASSWORD, ...).
set -Eeuo pipefail
while IFS='=' read -r k v; do [[ -n $k ]] && export "$k=$v"; done
# scram_verifier: same code as apps/demo/schema/00-bravo-init.sh
eval "$(sed -n '/^scram_verifier() {/,/^}/p; /^export_verifier() {/,/^}/p' /schema/00-bravo-init.sh)"
export_verifier APP_WRITER_PASSWORD BRAVO_APP_WRITER_VERIFIER
export_verifier REPLICATOR_PASSWORD BRAVO_REPLICATOR_VERIFIER
export_verifier EXECUTOR_CTL_PASSWORD BRAVO_EXECUTOR_CTL_VERIFIER
export_verifier EXECUTOR_OBSERVE_PASSWORD BRAVO_EXECUTOR_OBSERVE_VERIFIER
unset APP_WRITER_PASSWORD REPLICATOR_PASSWORD EXECUTOR_CTL_PASSWORD EXECUTOR_OBSERVE_PASSWORD
S=/schema/sql
# MANAGED_PRELUDE: session SQL before 01 (Cloud SQL: SET createrole_self_grant = 'set, inherit').
PRELUDE="${MANAGED_PRELUDE:-}"
case ${1:?mode} in
  dryrun)
    { echo 'BEGIN;'; echo '\set ON_ERROR_ROLLBACK on'; echo "$PRELUDE"
      for f in 01_schema.sql 02_roles.sql 03_src.sql; do echo "\\echo ==== $f"; grep -v '^\\set ON_ERROR_STOP' "$S/$f"; done
      echo 'ROLLBACK;'; } | psql --no-psqlrc -v ON_ERROR_STOP=0 -v attachment_store_id=src -e -f - 2>&1 ;;
  apply)
    { echo "$PRELUDE"; cat "$S/01_schema.sql" /variant/02_roles.sql "$S/03_src.sql"; } \
      | psql --no-psqlrc -v ON_ERROR_STOP=1 -v attachment_store_id=src -e -f - 2>&1 ;;
esac
