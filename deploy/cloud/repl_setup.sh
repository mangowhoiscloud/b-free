#!/usr/bin/env bash
# Forward replication src -> dst over the temporary link and the prepared reverse subscription
# (03-migration.md 4.1, CLOUD-E2E-CONTRACT.md 7). Runs as executor_ctl with OpenBao secrets.
#   repl_setup.sh status | setup | rearm      (output: JSON lines, also saved under runs/)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
"$CLOUD_DIR/render_topology.sh" >/dev/null
(cd "$REPO/platform/executor" && uv sync --quiet)
out="$RUNS/cloud-repl-$(date +%Y%m%dT%H%M%S)-${1:-status}.jsonl"
"$BAO_SH" with-executor -- env BRAVO_TOPOLOGY_FILE="$STATE/topology.json" \
  "$REPO/platform/executor/.venv/bin/python" -m bravo_executor.repl_setup "${1:-status}" "${@:2}" | tee "$out"
