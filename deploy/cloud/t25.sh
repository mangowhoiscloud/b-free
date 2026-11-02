#!/usr/bin/env bash
# T25: measured runs, one fault injection, one SAFE_HOLD test (EKS <-> K3s).
#   t25.sh measure [N]   N roundtrips (rearm between runs): raw write_pause_ms, median, max.
#                        Each run needs its own approvals: APPROVALS_FILE = JSON lines
#                        {"plan_id", "cutover", "failback"} from the platform API, one per run.
#   t25.sh fault         cutover, then destination app error responses (write block on the dst
#                        app = HTTP 503 / WS nack), detected by probing the front -> automatic S4
#   t25.sh safe-hold     cutover with the Executor killed right after the dst PgBouncer target
#                        switch (after the commit point) -> restart -> observe-only + SAFE_HOLD
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
executor_url; URL="$EXECUTOR_URL"
OUT="$RUNS/cloud-t25-$(date +%Y%m%dT%H%M%S)-${1:-measure}"; mkdir -p "$OUT"
latest_ev() { ls -dt "$RUNS"/cloud-*-"$1" 2>/dev/null | head -n 1; }

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
    SCENARIO=cutover "$CLOUD_DIR/run_e2e.sh" > "$OUT/cutover.log" 2>&1 || fail "cutover did not pass (see $OUT/cutover.log)"
    ev="$(latest_ev cutover)"; echo "{\"cutover_evidence\": \"$ev\"}" > "$OUT/context.json"
    log "fault: destination app write block (error responses) on ${DST_APP_ENV_ID}"
    ops entry "$DST_APP_ENV_ID" block > "$OUT/fault-injected.json"
    "$REPO/apps/demo/.venv/bin/python" "$REPO/deploy/pilot/scripts/demo_traffic.py" \
      --url "wss://${FRONT_APP}.mangowhois.ai/ws" --rate 5 --duration 10 --drain 5 > "$OUT/watch-probe.json" 2>/dev/null || true
    jq -c . "$OUT/watch-probe.json"
    if jq -e '(.unacked // 0) + (.nack_final // 0) > 0' "$OUT/watch-probe.json" >/dev/null; then
      log "WATCH judged FAIL (unacked writes on the destination front) -> automatic S4 failback"
      epoch="$(date +%s)"
      : "${APPROVAL_FAILBACK:?failback approval id from the platform API (the DB failback is never unapproved)}"
      body="$(jq -nc --arg p "$PLAN_ID" --arg a "$APPROVAL_FAILBACK" --argjson e "$epoch" \
        '{plan_id:$p, epoch:$e, approval_id:$a, write_pause_budget_ms:5000}')"
      id="$(curl -fsS -X POST "$URL/v1/failback" -H 'Content-Type: application/json' -H "Idempotency-Key: t25-fault-$(uuidgen)" -d "$body" | jq -r .run_id)"
      while [ "$(curl -fsS "$URL/v1/runs/$id" | jq -r .status)" = "RUNNING" ]; do sleep 1; done
      curl -fsS "$URL/v1/runs/$id" > "$OUT/failback-run.json"
      jq -c '{status, write_pause_ms, verdict: .receipt.verdict}' "$OUT/failback-run.json"
      [ "${SKIP_FRONT:-0}" = "1" ] || {
        fid="$(curl -fsS -X POST "$URL/v1/front/shift" -H 'Content-Type: application/json' -H "Idempotency-Key: t25-front-$(uuidgen)" -d '{"target_env":"src"}' | jq -r .run_id)"
        while [ "$(curl -fsS "$URL/v1/runs/$fid" | jq -r .status)" = "RUNNING" ]; do sleep 1; done
        curl -fsS "$URL/v1/runs/$fid" > "$OUT/front-back.json"; }
    else
      log "WATCH did not see errors: fault not effective"; fail "fault injection had no visible effect"
    fi
    log "fault cleared on the destination DB row (the env is now failed back)"
    ops entry "$DST_APP_ENV_ID" get > "$OUT/after.json" || true
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
  *) fail "usage: t25.sh measure [N] | fault | safe-hold" ;;
esac
