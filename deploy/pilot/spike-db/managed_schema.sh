#!/usr/bin/env bash
# Apply the chat schema to the spike managed DB (SPIKE_SIDE=rds|cloudsql) as its master user, through the link.
# psql 16 runs in a postgres:16.15 container (managed_init.sh); passwords go in on stdin only.
#   managed_schema.sh dryrun   stock 01/02/03_src in one transaction with ON_ERROR_ROLLBACK, then ROLLBACK
#   managed_schema.sh apply    01 + <side>_02_roles.sql + 03_src, for real
# (The RDS run on 2026-11-02 used an earlier stock 02_roles.sql with plain password variables; its
#  logs are in spike-rds-*/schema-*.log. This version follows the SCRAM-verifier stock file.)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
mode=${1:?usage: dryrun | apply}
hostaddr="$(docker run --rm --add-host host.docker.internal:host-gateway postgres:16.15 getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}')"
{ for r in app_writer replicator executor_ctl executor_observe; do
    printf '%s_PASSWORD=%s\n' "$(printf '%s' "$r" | tr '[:lower:]' '[:upper:]')" "$("$SPIKE_HERE/bao.sh" get "$M_SECRET" "$r")"
  done; printf 'PGPASSWORD=%s\n' "$(m_master_pw)"; } \
| docker run --rm -i --add-host host.docker.internal:host-gateway \
    -v "$SPIKE_REPO/apps/demo/schema:/schema:ro" -v "$SPIKE_TLS:/tls:ro" \
    -v "$SPIKE_HERE/${SPIKE_SIDE}_02_roles.sql:/variant/02_roles.sql:ro" -v "$SPIKE_HERE/managed_init.sh:/managed_init.sh:ro" \
    -e PGHOST="$M_HOST" -e PGHOSTADDR="$hostaddr" -e PGPORT="$M_LOCAL_PORT" -e PGDATABASE=chat -e PGUSER="$M_MASTER" \
    -e MANAGED_PRELUDE="${MANAGED_PRELUDE:-}" -e PGSSLMODE=verify-full -e PGSSLROOTCERT="/tls/$(basename "$M_CA")" -e PGCONNECT_TIMEOUT=10 \
    postgres:16.15 bash /managed_init.sh "$mode" > "$SPIKE_RUN/schema-$mode.log"
grep -B2 -E 'ERROR|WARNING|error:' "$SPIKE_RUN/schema-$mode.log" || echo "no errors"
