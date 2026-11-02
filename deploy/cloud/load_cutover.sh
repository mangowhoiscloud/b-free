#!/usr/bin/env bash
# One S3 cutover under load (NFR-1, NFR-2, T25: run k of 5). This script never starts a shift, a cutover
# or a failback and never creates approvals. People do that in the console: the operator starts, a
# different person approves (D-18). The script drives load, waits for the console-started runs of the
# plan, and records everything.
#
#   deploy/cloud/load_cutover.sh <plan-id> [label]
#
#   1. preflight: executor-0 up and not observe-only, forward replication streaming
#   2. load through the front (FRONT_URL, the LB the SHIFT moves): WRITE_RATE writes/s + READ_RATE reads/s from a
#      Job in the platform cluster (load_lib.sh; never from this Mac), OTLP metrics with bravo.plan_id=<plan-id>
#      (tools/loadgen/README.md "지표 계약"), node CPU sampled on the source, destination and platform
#   3. waits for the plan's shift run, then its cutover run (each up to WAIT_RUN_S), started in the console.
#      APPROVAL_ID (optional): accept only runs that carry this approval id
#   4. keeps load on the destination for HOLD_AFTER_S
#   5. FAILBACK=1 (default): waits for the plan's failback run too, so that the next run starts forward
#   6. stops load (SIGTERM, outstanding writes drain), NFR-2 on the final DB, run.json for the statistics
#      (python -m bravo_loadgen.cutover_stats docs/evidence/load/*-cutover-*/run.json)
#
# Env: FRONT_URL (https://<FRONT_APP>.mangowhois.ai), WRITE_RATE (50), READ_RATE (150), WORKERS (8),
#      WAIT_RUN_S (1800), HOLD_AFTER_S (60), FAILBACK (1), APPROVAL_ID.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$CLOUD_DIR/load_lib.sh"
PLAN_ID="${1:?plan id (the console plan whose shift/cutover/failback the operator starts)}"
LABEL="${2:-cutover}"
executor_url; URL="$EXECUTOR_URL"
FRONT_URL="${FRONT_URL:-https://${FRONT_APP}.mangowhois.ai}"
WRITE_RATE="${WRITE_RATE:-50}"; READ_RATE="${READ_RATE:-150}"; WORKERS="${WORKERS:-8}"
WAIT_RUN_S="${WAIT_RUN_S:-1800}"; HOLD_AFTER_S="${HOLD_AFTER_S:-60}"; FAILBACK="${FAILBACK:-1}"
EV="$REPO/docs/evidence/load/$(TZ=Asia/Seoul date +%Y%m%d-%H%M%S)-$LABEL"
mkdir -p "$EV"
log "evidence: $EV"
(cd "$REPO/tools/loadgen" && uv sync --quiet --frozen)  # NFR-2 comparison runs here (no load)
pids=()
cleanup() {
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done
  if [ -n "$LOADGEN_JOB" ]; then loadgen_job_stop; _lk delete job "$LOADGEN_JOB" --wait=false >/dev/null 2>&1 || true; fi
  [ -z "$EXECUTOR_PF_PID" ] || kill "$EXECUTOR_PF_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ---- 1. preflight -----------------------------------------------------------------------------
host="$(python3 -I -c 'import sys,urllib.parse;print(urllib.parse.urlsplit(sys.argv[1]).netloc)' "$FRONT_URL")"
jq -n --arg plan "$PLAN_ID" --arg label "$LABEL" --arg front "$FRONT_URL" --arg git "$(git -C "$REPO" rev-parse HEAD)" \
  --arg kst "$(TZ=Asia/Seoul date -Iseconds)" --arg colo "$(curl -fsS -m 10 "https://$host/cdn-cgi/trace" | sed -n 's/^colo=//p')" \
  --arg rates "$WRITE_RATE writes/s + $READ_RATE reads/s" --arg approval "${APPROVAL_ID:-}" \
  '{plan_id: $plan, label: $label, front: $front, git: $git, started_kst: $kst, edge_colo: $colo, load: $rates,
    expected_approval_id: (if $approval == "" then null else $approval end)}' > "$EV/context.json"
"$REPO/deploy/scripts/argo-state.sh" > "$EV/argo-state.txt" || true
curl -fsS "$URL/healthz" > "$EV/healthz.json"
jq -e '.observe_only == false' "$EV/healthz.json" >/dev/null || fail "executor-0 is observe-only: $(cat "$EV/healthz.json")"
curl -fsS "$URL/v1/readiness" > "$EV/readiness-before.json"; jq -c . "$EV/readiness-before.json"
jq -e '.direction == "forward" and .sub_state == "streaming"' "$EV/readiness-before.json" >/dev/null \
  || fail "forward replication is not streaming; the run needs the forward state (after a failback)"

# ---- 2. load ----------------------------------------------------------------------------------
for ctx in "$SRC_CONTEXT" "$DST_CONTEXT" "$PLATFORM_CONTEXT"; do
  "$CLOUD_DIR/load_sample.sh" "$ctx" "$EV/node-cpu-$ctx.jsonl" 5 & pids+=("$!")
done
RUN_ID="$(uuidgen | tr 'A-Z' 'a-z')"
loadgen_job_start "$RUN_ID" "$FRONT_URL" "$WRITE_RATE" "$READ_RATE" 7200 "$WORKERS" "$PLAN_ID"
t0="$(date -u +%Y-%m-%dT%H:%M:%S)"
log "load running (run $RUN_ID, plan $PLAN_ID, $WRITE_RATE w/s + $READ_RATE r/s via $FRONT_URL)"
log "operator: start SHIFT and then CUTOVER for plan $PLAN_ID in the console (a different person approves)"

# ---- 3-5. wait for the console-started runs ---------------------------------------------------
wait_plan_run() {  # wait_plan_run <kind> <out> -> status on stdout
  local deadline=$(( $(date +%s) + WAIT_RUN_S )) id="" st aid
  while [ -z "$id" ]; do
    id="$(curl -fsS "$URL/v1/runs?kind=$1&limit=10" | jq -r --arg after "$t0" --arg plan "$PLAN_ID" \
      '[.[] | select(.started_at > $after and .plan_id == $plan)] | sort_by(.started_at) | last | .run_id // empty')"
    if [ -z "$id" ]; then
      [ "$(date +%s)" -lt "$deadline" ] || { echo "NOT_STARTED"; return 0; }
      sleep 3
    fi
  done
  log "$1 run $id started" >&2
  while :; do
    curl -fsS "$URL/v1/runs/$id" > "$2"
    st="$(jq -r .status "$2")"
    [ "$st" != "RUNNING" ] && break
    sleep 1
  done
  aid="$(jq -r '[.stages[]?.detail.approval_id? // empty] | first // ""' "$2")"
  if [ -n "${APPROVAL_ID:-}" ] && [ "$aid" != "$APPROVAL_ID" ]; then
    log "$1 run $id carries approval '$aid', expected '$APPROVAL_ID'" >&2
  fi
  jq -c '{kind, status, write_pause_ms, stages: [.stages[] | {name, ms, result}]}' "$2" >&2
  echo "$st"
}
shift_status="$(wait_plan_run shift "$EV/shift-run.json")"
log "shift: $shift_status"
cut_status="NOT_STARTED"
if [ "$shift_status" = "DONE" ]; then
  cut_status="$(wait_plan_run cutover "$EV/cutover-run.json")"
  log "cutover: $cut_status"
fi
fb_status="skipped"
if [ "$cut_status" = "DONE" ]; then
  curl -fsS "$URL/v1/readiness" > "$EV/readiness-after-cutover.json"
  log "holding load on the destination for ${HOLD_AFTER_S}s"
  sleep "$HOLD_AFTER_S"
  if [ "$FAILBACK" = "1" ]; then
    log "operator: start FAILBACK for plan $PLAN_ID in the console (failback approval by a different person)"
    fb_status="$(wait_plan_run failback "$EV/failback-run.json")"
    log "failback: $fb_status"
  fi
fi

# ---- 6. stop, NFR-2, record ---------------------------------------------------------------------
log "stopping load (SIGTERM: no new requests, outstanding ones drain)"
loadgen_job_stop
loadgen_job_collect "$EV" 900
lg_rc="$LOADGEN_RC"; LOADGEN_JOB=""
curl -fsS "$URL/v1/readiness" > "$EV/readiness-after.json" || true
for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done; pids=()
final=src; [ "$cut_status" = "DONE" ] && [ "$fb_status" != "DONE" ] && final=dst
"$CLOUD_DIR/load_nfr2.sh" "$EV" "$final" > /dev/null 2>&1 || true
other=dst; [ "$final" = "dst" ] && other=src
"$CLOUD_DIR/load_nfr2.sh" "$EV" "$other" > /dev/null 2>&1 || true  # the replicated copy, for information
gzip -9 "$EV/requests.jsonl" "$EV/events.jsonl" "$EV"/rows-*.json 2>/dev/null || true
approval_of() { [ -f "$1" ] && jq '[.stages[]?.detail.approval_id? // empty] | first // null' "$1" || echo null; }
run_of() { [ -f "$1" ] && jq '{run_id, status, write_pause_ms, stages: [.stages[] | {name, ms, result}],
  verdict: (.receipt.verdict // null)}' "$1" || echo null; }
jq -n --arg plan "$PLAN_ID" --arg final "$final" --argjson lg "$lg_rc" \
  --argjson shift "$(run_of "$EV/shift-run.json")" --argjson cut "$(run_of "$EV/cutover-run.json")" \
  --argjson fb "$(run_of "$EV/failback-run.json")" \
  --argjson a_shift "$(approval_of "$EV/shift-run.json")" --argjson a_cut "$(approval_of "$EV/cutover-run.json")" \
  --argjson a_fb "$(approval_of "$EV/failback-run.json")" \
  --slurpfile s "$EV/summary.json" --slurpfile n "$EV/nfr2-$final.json" '{
    plan_id: $plan, approval_ids: {shift: $a_shift, cutover: $a_cut, failback: $a_fb},
    shift: $shift, cutover: $cut, failback: $fb,
    write_pause_ms: ($cut.write_pause_ms // null), final_db: $final, loadgen_exit: $lg,
    load: ($s[0] | {run_id, writes: (.writes | {sent, ok, failed, replay, retried_requests, achieved_ok_per_s, latency_ms, longest_ok_gap_ms, served_by}),
                    reads: (.reads | {sent, ok, failed, achieved_ok_per_s, latency_ms, served_by}), client}),
    nfr2: ($n[0] // null)}' > "$EV/run.json"
jq -c '{plan_id, approval_ids, cutover: .cutover.status, write_pause_ms, nfr2_ok: .nfr2.ok}' "$EV/run.json"
[ "$cut_status" = "DONE" ] && jq -e '.nfr2.ok == true' "$EV/run.json" >/dev/null && log "PASS ($EV)" || fail "see $EV"
