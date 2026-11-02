#!/usr/bin/env bash
# T25: measured runs, one fault injection, one SAFE_HOLD test (EKS <-> K3s).
#   t25.sh measure [N]   N roundtrips (rearm between runs): raw write_pause_ms, median, max.
#                        Each run needs its own approvals: APPROVALS_FILE = JSON lines
#                        {"plan_id", "cutover", "failback"} from the platform API, one per run.
#                        Contract 15 §10.1: the Executor now refuses a cutover whose approval was not
#                        consumed by a console start (APPROVAL_NOT_STARTED) or whose shift is not DONE
#                        (SHIFT_NOT_DONE). Measured runs go through the console: SHIFT -> CUTOVER -> failback.
#   t25.sh fault [rate]  S4 trigger only (contract 15 §10.8): destination app pods answer HTTP 5xx at
#                        <rate> (default 0.3) through their admin port 8081 (PUT /admin/fault, fault.sh),
#                        then 20 writes through the front show the errors. No cutover, no failback: the
#                        failback starts only from the console after a human failback approval.
#   t25.sh fault-clear   sets the destination fault rate back to 0 and shows 20 writes through the front
#   t25.sh safe-hold     cutover with the Executor killed right after the dst PgBouncer target
#                        switch (after the commit point) -> restart -> observe-only + SAFE_HOLD
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
case "${1:-measure}" in fault|fault-clear) ;; *) executor_url; URL="$EXECUTOR_URL" ;; esac
OUT="$RUNS/cloud-t25-$(date +%Y%m%dT%H%M%S)-${1:-measure}"; mkdir -p "$OUT"
latest_ev() { ls -dt "$RUNS"/cloud-*-"$1" 2>/dev/null | head -n 1; }
# N real chat writes through the front (room fault-probe): status and X-Served-By per environment.
front_writes() {
  python3 - "https://${FRONT_APP}.mangowhois.ai/api/rooms/fault-probe/messages" "$1" <<'PY'
import json, sys, time, urllib.error, urllib.request, uuid
url, n = sys.argv[1], int(sys.argv[2])
out = {"url": url, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sent": n, "by_env": {}, "errors_by_env": {}, "statuses": {}}
for i in range(n):
    req = urllib.request.Request(url, method="POST", data=json.dumps({"author": "bravo-fault", "body": f"fault probe {i}"}).encode(),
                                 headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, env = r.status, r.headers.get("X-Served-By")
    except urllib.error.HTTPError as e:
        status, env = e.code, e.headers.get("X-Served-By")
    except Exception as e:  # noqa: BLE001
        status, env = type(e).__name__, None
    env = env or "none"
    out["by_env"][env] = out["by_env"].get(env, 0) + 1
    out["statuses"][str(status)] = out["statuses"].get(str(status), 0) + 1
    if status not in (200, 201):
        out["errors_by_env"][env] = out["errors_by_env"].get(env, 0) + 1
    time.sleep(0.1)
print(json.dumps(out))
PY
}

case "${1:-measure}" in
  measure)
    n="${2:-5}"; : > "$OUT/runs.jsonl"
    for i in $(seq 1 "$n"); do
      if [ "$i" -gt 1 ]; then "$CLOUD_DIR/repl_setup.sh" rearm > "$OUT/rearm-$i.jsonl" || fail "rearm $i failed"; fi
      : "${APPROVALS_FILE:?JSON lines of platform approvals, one per run}"
      line="$(sed -n "${i}p" "$APPROVALS_FILE")"; [ -n "$line" ] || fail "no approvals for run $i"
      PLAN_ID="$(jq -r .plan_id <<< "$line")" APPROVAL_CUTOVER="$(jq -r .cutover <<< "$line")" \
        APPROVAL_FAILBACK="$(jq -r .failback <<< "$line")" SCENARIO=roundtrip "$CLOUD_DIR/run_e2e.sh" \
        > "$OUT/run-$i.log" 2>&1 || log "run $i did not pass (see $OUT/run-$i.log)"
      ev="$(latest_ev roundtrip)"
      jq -c --arg i "$i" --arg ev "$ev" '. + {i: ($i|tonumber), evidence: $ev}' "$ev/summary.json" | tee -a "$OUT/runs.jsonl"
    done
    jq -s '
      def med: sort | if length == 0 then null elif length % 2 == 1 then .[length/2|floor]
               else (.[length/2-1] + .[length/2]) / 2 end;
      {runs: length,
       passed: map(select(.ok)) | length,
       cutover_write_pause_ms: (map(.cutover.write_pause_ms | select(. != null))),
       failback_write_pause_ms: (map(.failback.write_pause_ms | select(. != null)))}
      | . + {cutover_median_ms: (.cutover_write_pause_ms | med), cutover_max_ms: (.cutover_write_pause_ms | max),
             failback_median_ms: (.failback_write_pause_ms | med), failback_max_ms: (.failback_write_pause_ms | max)}
    ' "$OUT/runs.jsonl" | tee "$OUT/t25-summary.json"
    ;;
  fault)
    rate="${2:-0.3}"
    log "fault: destination app pods answer HTTP 5xx at rate $rate (admin port 8081); no cutover, no failback"
    "$CLOUD_DIR/fault.sh" dst "$rate" | tee "$OUT/fault-injected.jsonl"
    front_writes 20 | tee "$OUT/front-writes.json"
    log "next: the console shows the errors; failback = approver approves the failback request, operator starts it"
    ;;
  fault-clear)
    "$CLOUD_DIR/fault.sh" dst 0 | tee "$OUT/fault-cleared.jsonl"
    front_writes 20 | tee "$OUT/front-writes.json"
    ;;
  safe-hold)
    executor_set_fault "pgbouncer/dst/active:ACTIVATE_TARGET"
    SCENARIO=cutover SKIP_FRONT=1 "$CLOUD_DIR/run_e2e.sh" > "$OUT/cutover.log" 2>&1 || true
    executor_clear_fault; URL="$EXECUTOR_URL"
    curl -fsS "$URL/healthz" | tee "$OUT/healthz-after-restart.json"
    id="$(curl -fsS "$URL/v1/runs?kind=cutover&limit=1" | jq -r '.[0].run_id')"
    curl -fsS "$URL/v1/runs/$id" > "$OUT/run.json"
    jq -c '{status, stages: [.stages[] | .name], unknown: [.effects[] | select(.state != "CONFIRMED_APPLIED") | "\(.resource) \(.action) \(.state)"]}' "$OUT/run.json"
    jq -e '.status == "SAFE_HOLD"' "$OUT/run.json" >/dev/null && log "PASS SAFE_HOLD (evidence $OUT)" || fail "run is not SAFE_HOLD"
    ;;
  *) fail "usage: t25.sh measure [N] | fault [rate] | fault-clear | safe-hold" ;;
esac
