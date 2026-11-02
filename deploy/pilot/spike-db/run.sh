#!/usr/bin/env bash
# Run spike.py with DSNs and passwords in its environment only (values from OpenBao and the
# RDS-managed secret, never on argv or in files).   run.sh <spike.py args...>
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
B="$SPIKE_HERE/bao.sh"
export SPIKE_RUN M_SIDE_LABEL="$SPIKE_SIDE" M_NAME="$M_HOST" M_LINK_PORT="$M_LOCAL_PORT" M_LOAD_PORT="${M_LOAD_PORT:-}"
# address of the laptop as seen from the spike PG container (Docker Desktop host gateway)
export M_LINK_HOSTADDR="$(docker exec bravo-spike-pg getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}')"
if [[ $SPIKE_SIDE == rds ]]; then
  export C_LINK_HOST="$(kubectl --context "$CTX" -n "$NS" get pod -l app.kubernetes.io/name=pilot-pg-link \
    -o jsonpath='{.items[0].status.podIP}')"
else
  export C_LINK_HOST="$(tfout link_vm_ip)"
fi
for r in executor_ctl app_writer replicator executor_observe; do
  R="$(printf '%s' "$r" | tr '[:lower:]' '[:upper:]')"
  export "M_DSN_${R}=$(m_dsn "$r")" "C_DSN_${R}=$(pg_dsn "$r")"
  export "M_PW_${R}=$("$B" get "$M_SECRET" "$r")" "C_PW_${R}=$("$B" get envs/spike-db/pilot-pg "$r")"
done
export M_DSN_MASTER="$(m_dsn "$M_MASTER")" M_PW_MASTER="$(m_master_pw)"
export C_DSN_POSTGRES="$(pg_dsn postgres)" C_PW_POSTGRES="$("$B" get envs/spike-db/pilot-pg postgres)"
export PGB_ADMIN_PW="$("$B" get envs/spike-db/pgbouncer pgb_admin 2>/dev/null || true)"
exec "$SPIKE_REPO/platform/executor/.venv/bin/python" "$SPIKE_HERE/spike.py" "$@"
