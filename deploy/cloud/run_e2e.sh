#!/usr/bin/env bash
# S3 cutover and S4 failback across source EKS and on-prem K3s, under load (T17, T8, NFR-1/2, S4-AC1).
# The Executor is executor-0 on the platform EKS. The cutover and the failback are started by people
# in the console (approvals, D-18): this script never starts them. It drives load and the front shift
# (executor-0 POST /v1/front/shift), waits for the console-started runs and verifies the outcome.
#
#   1. checks: link up, executor-0 up (not observe-only), forward replication streaming, controls
#   2. loadgen through the source PgBouncer (app_writer, DSN from OpenBao) and demo-app traffic
#      through the public front wss://<FRONT_APP>.<zone>/ws
#   3. front shift to dst (CNAME flip; 10/50% recorded as "LB 구독 필요" when there is no LB)
#   4. cutover (step 2 blocks writes through each app's admin switch; step 7 retargets both
#      PgBouncers with pods/exec; 1b freezes attachments and copies objects S3 -> RGW when the
#      object stores are configured)
#   5. load on dst for HOLD_S; 장애 주입 (spec S4 "목적지 앱 오류 응답": dst app pods answer FAULT_RATE of HTTP
#      with 500, deploy/cloud/fault.sh); failback started in the console; front back to src in one 100 % step
#      (a rollback, not a canary: a 10 % step would still send 90 % to the failing dst); fault cleared
#   6. NFR-2 against the final DB, S4-AC1, every run record exported to the evidence folder
#
# WAIT_RUN_S (default 900): how long to wait for the console to start each run.
# Env: SKIP_FRONT=1 skips the front shifts (recorded), HOLD_S (default 20), LOAD_S (default 120),
#      FAULT_RATE (default 0.3; 0 = no fault injection),
#      CUTOVER_AFTER_S (default 15), BUDGET_MS (default 5000), SCENARIO (roundtrip|cutover).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
executor_url; URL="$EXECUTOR_URL"
SCENARIO="${SCENARIO:-roundtrip}"
WAIT_RUN_S="${WAIT_RUN_S:-900}"
HOLD_S="${HOLD_S:-20}"; LOAD_S="${LOAD_S:-3600}"; CUTOVER_AFTER_S="${CUTOVER_AFTER_S:-15}"; BUDGET_MS="${BUDGET_MS:-5000}"
EV="$RUNS/cloud-$(date +%Y%m%dT%H%M%S)-$SCENARIO"
mkdir -p "$EV"
echo "{\"link\": \"$LINK_LABEL\", \"src\": \"$SRC_CONTEXT\", \"dst\": \"$DST_CONTEXT\", \"scenario\": \"$SCENARIO\"}" > "$EV/run-context.json"
log "evidence: $EV"

post() {  # post <path> <body> <key> <out>
  local code
  code="$(curl -sS -o "$4" -w '%{http_code}' -X POST "$URL$1" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $3" -d "$2")"
  [ "$code" = "202" ] || fail "POST $1 -> $code: $(cat "$4")"
  jq -r .run_id "$4"
}
wait_run() {  # wait_run <run_id> <out> [timeout_s]
  local deadline=$(( $(date +%s) + ${3:-300} )) st
  while :; do
    curl -fsS "$URL/v1/runs/$1" > "$2"
    st="$(jq -r .status "$2")"
    [ "$st" != "RUNNING" ] && break
    [ "$(date +%s)" -lt "$deadline" ] || fail "run $1 still RUNNING"
    sleep 1
  done
  jq -c '{kind, status, write_pause_ms, stages: [.stages[] | {name, ms, result}], verdict: .receipt.verdict}' "$2" >&2
  echo "$st"
}
front_shift() {  # front_shift <env> <tag> [steps-json]
  if [ "${SKIP_FRONT:-0}" = "1" ]; then
    echo '{"skipped": true, "reason": "SKIP_FRONT=1"}' > "$EV/front-$2.json"; log "front shift to $1 skipped"; return 0
  fi
  local id st
  id="$(post /v1/front/shift "{\"target_env\": \"$1\"${3:+, \"steps\": $3}}" "front-$2-$(uuidgen)" \
    "$EV/front-$2-accepted.json")"
  st="$(wait_run "$id" "$EV/front-$2.json" 600 | tail -n 1)"
  log "front shift to $1: $st ($(jq -c '[.stages[]? | {name, ms, result}]' "$EV/front-$2.json"))"
}

# ---- 1. checks --------------------------------------------------------------------------
# executor-0 reaches every DB, PgBouncer, k8s API and write-block switch through the D-16 links; the
# laptop keeps one forward, for the load generator only (link.sh load, labelled 임시 링크).
"$CLOUD_DIR/link.sh" load
for _ in $(seq 1 30); do
  jq -e '.state == "up"' "$STATE/src-pgb.json" >/dev/null 2>&1 && break; sleep 1
done
"$CLOUD_DIR/link.sh" status > "$EV/link-before.jsonl" || true
jq -e '.state == "up"' "$STATE/src-pgb.json" >/dev/null || fail "loadgen forward src-pgb is not up (link.sh load)"
curl -fsS "$URL/healthz" > "$EV/healthz.json" || fail "executor-0 not reachable"
jq -e '.observe_only == false' "$EV/healthz.json" >/dev/null || fail "executor is observe-only: $(cat "$EV/healthz.json")"
curl -fsS "$URL/v1/readiness" > "$EV/readiness-before.json"; jq -c . "$EV/readiness-before.json"
jq -e '.direction == "forward" and .sub_state == "streaming"' "$EV/readiness-before.json" >/dev/null \
  || fail "forward replication is not streaming (repl_setup.sh setup)"
curl -fsS "$URL/v1/controls" > "$EV/controls.json"; jq -c . "$EV/controls.json"

# ---- 2. load ----------------------------------------------------------------------------
dsn_of() {  # dsn_of <pool> -> DSN on stdout (shell variable only, never a file)
  "$BAO_SH" with-executor -- env BRAVO_TOPOLOGY_FILE="$STATE/topology.json" \
    "$REPO/platform/executor/.venv/bin/python" -m bravo_executor.topology dsn "$1" app
}
LOADGEN_DSN="$(dsn_of src)"; export LOADGEN_DSN
(cd "$REPO/tools/loadgen" && uv sync --quiet)
LOADGEN_RATE="$LOADGEN_RATE" LOADGEN_DURATION_S="$LOAD_S" LOADGEN_VERIFY=1 \
  "$REPO/tools/loadgen/.venv/bin/python" -m bravo_loadgen --attempt-timeout 15 --max-attempts 40 \
  > "$EV/loadgen.stdout" 2> "$EV/loadgen.stderr" &
loadgen_pid=$!
demo_pid=""
ZONE_HOST="${FRONT_APP}.mangowhois.ai"
if [ "${SKIP_DEMO:-0}" != "1" ]; then
  (cd "$REPO/apps/demo" && uv sync --quiet)
  "$REPO/apps/demo/.venv/bin/python" "$REPO/deploy/pilot/scripts/demo_traffic.py" --url "wss://$ZONE_HOST/ws" \
    --rate "$DEMO_RATE" --duration "$LOAD_S" --drain 60 --verify-dsn "$LOADGEN_DSN" \
    > "$EV/demo-traffic.json" 2> "$EV/demo-traffic.stderr" &
  demo_pid=$!
fi
unset LOADGEN_DSN
log "load started (loadgen ${LOADGEN_RATE}/s via src PgBouncer, demo ${DEMO_RATE}/s via wss://$ZONE_HOST)"
sleep "$CUTOVER_AFTER_S"

# ---- 3-5. shift, cutover, failback --------------------------------------------------------
front_shift dst to-dst
# Runs started in the console appear on executor-0; this waits for the next one of a kind.
wait_console_run() {  # wait_console_run <kind> <after-iso> <out> -> run status on stdout
  local deadline=$(( $(date +%s) + WAIT_RUN_S )) id=""
  log "waiting for a $1 run started in the console (up to ${WAIT_RUN_S}s)"
  while [ -z "$id" ]; do
    id="$(curl -fsS "$URL/v1/runs?kind=$1&limit=5" | jq -r --arg after "$2" '[.[] | select(.started_at > $after)] | last | .run_id // empty')"
    [ -n "$id" ] || { [ "$(date +%s)" -lt "$deadline" ] || { echo "NOT_STARTED"; return 0; }; sleep 3; }
  done
  wait_run "$id" "$3" 1800 | tail -n 1
}
t_cut="$(date -u +%Y-%m-%dT%H:%M:%S)"
cut_status="$(wait_console_run cutover "$t_cut" "$EV/cutover-run.json")"
log "cutover: $cut_status"
fb_status="skipped"
if [ "$SCENARIO" = "roundtrip" ] && [ "$cut_status" = "DONE" ]; then
  curl -fsS "$URL/v1/readiness" > "$EV/readiness-after-cutover.json"
  sleep "$HOLD_S"
  if [ "${FAULT_RATE:-0.3}" != "0" ]; then
    log "장애 주입: dst app HTTP 5xx rate ${FAULT_RATE:-0.3} (spec S4 목적지 앱 오류 응답)"
    "$CLOUD_DIR/fault.sh" dst "${FAULT_RATE:-0.3}" > "$EV/fault-on.jsonl"
    for _ in $(seq 1 40); do  # what a user of the front sees during the fault (evidence only)
      curl -s -o /dev/null -w '%{http_code}\n' -m 10 "https://$ZONE_HOST/api/config?_fault_probe=$RANDOM"
    done | sort | uniq -c | awk '{printf "{\"http\": %s, \"count\": %s}\n", $2, $1}' > "$EV/fault-observed.jsonl"
    jq -c . "$EV/fault-observed.jsonl" >&2
  fi
  log "load on dst; failback is manual from the console (D-18)"
  fb_status="$(wait_console_run failback "$t_cut" "$EV/failback-run.json")"
  log "failback: $fb_status"
  [ "$fb_status" != "DONE" ] || front_shift src to-src "[100]"
  [ "${FAULT_RATE:-0.3}" = "0" ] || "$CLOUD_DIR/fault.sh" dst 0 > "$EV/fault-off.jsonl"
fi

# ---- 6. verification -----------------------------------------------------------------------
log "stopping load (SIGTERM: no new requests, outstanding ones drain, then NFR-2 is checked)"
kill -TERM "$loadgen_pid" 2>/dev/null || true
[ -z "$demo_pid" ] || kill -TERM "$demo_pid" 2>/dev/null || true
wait "$loadgen_pid" && loadgen_exit=0 || loadgen_exit=$?
tail -n 1 "$EV/loadgen.stdout" > "$EV/loadgen-summary.json" || true
demo_exit=0; [ -n "$demo_pid" ] && { wait "$demo_pid" || demo_exit=$?; }
curl -fsS "$URL/v1/readiness" > "$EV/readiness-after.json" || true
"$CLOUD_DIR/link.sh" status > "$EV/link-after.jsonl" || true
if [ "$fb_status" = "DONE" ]; then
  cut_last="$(jq '.receipt.sequences[] | select(.name == "messages_id_seq") | .origin_last' "$EV/cutover-run.json")"
  fb_last="$(jq '.receipt.sequences[] | select(.name == "messages_id_seq") | .origin_last' "$EV/failback-run.json")"
  # Read inside executor-0 (in-cluster topology, delivered secrets, D-16 DB links), as executor_observe.
  kubectl --context "$PLATFORM_CONTEXT" -n bravo-control exec -i executor-0 -c executor -- \
    env CUT_LAST="$cut_last" FB_LAST="$fb_last" python - > "$EV/s4-ac1.json" <<'PY'
import asyncio, json, os
from pathlib import Path
from psycopg import AsyncConnection
from bravo_executor.topology import FileSecrets, settings_from_topology

async def main() -> None:
    secrets = FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"]))
    s = await settings_from_topology(Path(os.environ["BRAVO_TOPOLOGY_FILE"]), secrets.field, "unused")
    q = ("SELECT count(*), coalesce(sum(hashtext(idem_key::text)::bigint), 0) FROM messages "
         "WHERE id > %s AND id <= %s")
    out = {}
    for side, dsn in (("dst", s.dst_observe_dsn), ("src", s.src_observe_dsn)):
        async with await AsyncConnection.connect(dsn, autocommit=True) as c:
            row = await (await c.execute(q, (int(os.environ["CUT_LAST"]), int(os.environ["FB_LAST"])))).fetchone()
            out[side] = {"rows": row[0], "key_hash_sum": int(row[1])}
    out["match"] = out["dst"] == out["src"] and out["dst"]["rows"] > 0
    print(json.dumps(out))

asyncio.run(main())
PY
  jq -c . "$EV/s4-ac1.json"
fi

ok=1
[ "$cut_status" = "DONE" ] || ok=0
[ "$cut_status" != "NOT_STARTED" ] || log "no cutover was started in the console within ${WAIT_RUN_S}s"
[ "$SCENARIO" != "roundtrip" ] || [ "$fb_status" = "DONE" ] || ok=0
jq -e '.nfr2.ok == true' "$EV/loadgen-summary.json" >/dev/null || ok=0
[ -z "$demo_pid" ] || jq -e '.nfr2.ok == true and .unacked == 0' "$EV/demo-traffic.json" >/dev/null || ok=0
[ "$fb_status" != "DONE" ] || jq -e '.match == true' "$EV/s4-ac1.json" >/dev/null || ok=0
jq -n --slurpfile cut "$EV/cutover-run.json" --arg fb "$fb_status" --arg ok "$ok" \
  '{ok: ($ok == "1"), cutover: {status: $cut[0].status, write_pause_ms: $cut[0].write_pause_ms,
    verdict: $cut[0].receipt.verdict}, failback_status: $fb}' > "$EV/summary.json"
if [ -f "$EV/failback-run.json" ]; then
  jq --slurpfile fb "$EV/failback-run.json" '.failback = {status: $fb[0].status,
    write_pause_ms: $fb[0].write_pause_ms, verdict: $fb[0].receipt.verdict}' "$EV/summary.json" > "$EV/summary.tmp" \
    && mv "$EV/summary.tmp" "$EV/summary.json"
fi
jq -c . "$EV/summary.json"; jq -c '.summary, .nfr2' "$EV/loadgen-summary.json" 2>/dev/null || true
[ -z "$demo_pid" ] || jq -c . "$EV/demo-traffic.json" 2>/dev/null || true
[ "$ok" = "1" ] && log "PASS  evidence: $EV" || fail "see $EV"
