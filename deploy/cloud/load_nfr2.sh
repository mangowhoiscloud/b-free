#!/usr/bin/env bash
# NFR-2 for an app-mode load run (tools/loadgen --app-url). Reads every row of the run's room from one
# chat DB inside executor-0, as executor_observe over the D-16 DB link (no DB credential leaves the
# platform), then compares them with the run's request log (python -m bravo_loadgen.nfr2).
#
#   deploy/cloud/load_nfr2.sh <run-dir> [src|dst]     (run-dir has summary.json and requests.jsonl)
#
# Writes <run-dir>/rows-<side>.json and <run-dir>/nfr2-<side>.json. Exit 0 only when NFR-2 holds.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
DIR="${1:?run dir}"; SIDE="${2:-src}"
case "$SIDE" in src|dst) ;; *) fail "side must be src or dst" ;; esac
ROOM="$(jq -r .room "$DIR/summary.json")"
[[ "$ROOM" =~ ^load-[a-z0-9-]+$ ]] || fail "summary.json has no app-mode room: $ROOM"
kubectl --context "$PLATFORM_CONTEXT" -n bravo-control exec -i executor-0 -c executor -- \
  env ROOM="$ROOM" SIDE="$SIDE" python - > "$DIR/rows-$SIDE.json" <<'PY'
import asyncio, json, os
from datetime import datetime, timezone
from pathlib import Path
from psycopg import AsyncConnection
from bravo_executor.topology import FileSecrets, settings_from_topology

async def main() -> None:
    secrets = FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"]))
    s = await settings_from_topology(Path(os.environ["BRAVO_TOPOLOGY_FILE"]), secrets.field, "unused")
    dsn = s.src_observe_dsn if os.environ["SIDE"] == "src" else s.dst_observe_dsn
    async with await AsyncConnection.connect(dsn, autocommit=True) as c:
        cur = await c.execute("SELECT body, count(*) FROM messages WHERE room = %s GROUP BY body", (os.environ["ROOM"],))
        rows = {b: int(n) for b, n in await cur.fetchall()}
        seq = (await (await c.execute("SELECT last_value FROM messages_id_seq")).fetchone())[0]
    print(json.dumps({"side": os.environ["SIDE"], "room": os.environ["ROOM"], "role": "executor_observe",
                      "read_at": datetime.now(timezone.utc).isoformat(), "messages_id_seq": int(seq), "rows": rows}))

asyncio.run(main())
PY
jq -c '{side, room, read_at, distinct_bodies: (.rows | length), rows: ([.rows[]] | add // 0)}' "$DIR/rows-$SIDE.json" >&2
"$REPO/tools/loadgen/.venv/bin/python" -m bravo_loadgen.nfr2 --requests "$DIR/requests.jsonl" \
  --rows "$DIR/rows-$SIDE.json" > "$DIR/nfr2-$SIDE.json" && rc=0 || rc=$?
jq -c . "$DIR/nfr2-$SIDE.json"
exit "$rc"
