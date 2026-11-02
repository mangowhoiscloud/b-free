#!/usr/bin/env bash
# Load generator as a Job in the platform cluster (tools/loadgen/k8s/job.yaml), sourced after lib.sh by
# load_run.sh, load_cutover.sh and exit_drill.sh. The operator Mac only applies the Job, copies its files and
# checks NFR-2: it never opens the load's sockets itself.
#
#   loadgen_job_start <run_id> <app_url> <write_rate> <read_rate> <duration_s> <workers> [plan_id]
#   loadgen_job_stop                 SIGTERM: no new requests, outstanding writes drain
#   loadgen_job_collect <dir>        waits for the end, copies summary/requests/timeline/events, deletes the Job
LOADGEN_JOB_TMPL="$REPO/tools/loadgen/k8s/job.yaml"
LOADGEN_NS=bravo-control
LOADGEN_POD=""; LOADGEN_JOB=""
_lk() { kubectl --context "$PLATFORM_CONTEXT" -n "$LOADGEN_NS" "$@"; }

loadgen_job_start() {
  local run_id="$1" url="$2" w="$3" r="$4" dur="$5" workers="$6" plan="${7:-}"
  [[ "$url" =~ ^https://[a-z0-9.-]+$ ]] || fail "loadgen: app url must be https://<host>: $url"
  [[ "$run_id" =~ ^[a-z0-9-]+$ ]] || fail "loadgen: bad run id"
  [[ -z "$plan" || "$plan" =~ ^[0-9a-f-]+$ ]] || fail "loadgen: bad plan id"
  LOADGEN_JOB="loadgen-${run_id:0:8}"
  sed -e "s#@RUN8@#${run_id:0:8}#g" -e "s#@RUN_ID@#$run_id#g" -e "s#@APP_URL@#$url#g" \
      -e "s#@WRITE_RATE@#$w#g" -e "s#@READ_RATE@#$r#g" -e "s#@DURATION_S@#$dur#g" -e "s#@WORKERS@#$workers#g" \
      -e "s#@PLAN_ID@#$plan#g" -e "s#@DEADLINE_S@#$(( ${dur%.*} + 3600 ))#g" "$LOADGEN_JOB_TMPL" | _lk apply -f - >/dev/null
  for _ in $(seq 1 60); do
    LOADGEN_POD="$(_lk get pod -l "bravo.io/run-id=$run_id" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    [ -n "$LOADGEN_POD" ] && break; sleep 2
  done
  [ -n "$LOADGEN_POD" ] || fail "loadgen: no Pod for job $LOADGEN_JOB"
  _lk wait --for=condition=Ready "pod/$LOADGEN_POD" --timeout=300s >/dev/null || fail "loadgen: pod $LOADGEN_POD not running"
  log "loadgen job $LOADGEN_JOB pod $LOADGEN_POD running ($w writes/s + $r reads/s, ${dur}s, $workers workers, $url)"
}

loadgen_job_stop() {
  [ -n "$LOADGEN_POD" ] && _lk exec "$LOADGEN_POD" -c loadgen -- sh -c 'kill -TERM 1' >/dev/null 2>&1 || true
}

loadgen_job_collect() {  # loadgen_job_collect <dir> [timeout_s]
  local dir="$1" deadline=$(( $(date +%s) + ${2:-7200} )) stopped=0 f
  until _lk exec "$LOADGEN_POD" -c loadgen -- test -f /out/done 2>/dev/null; do
    if [ "$(_lk get pod "$LOADGEN_POD" -o jsonpath='{.status.phase}')" != "Running" ]; then
      _lk get pod "$LOADGEN_POD" -o json | jq '{phase: .status.phase, state: .status.containerStatuses[0].state,
        resources: .spec.containers[0].resources}' > "$dir/loadgen-pod-failed.json"
      fail "loadgen: pod $LOADGEN_POD ended before its files were copied ($(jq -c '.state' "$dir/loadgen-pod-failed.json"))"
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      [ "$stopped" = 0 ] || fail "loadgen: pod $LOADGEN_POD did not end after SIGTERM"
      log "loadgen: run did not end in time; stopping it"; loadgen_job_stop; stopped=1
      deadline=$(( $(date +%s) + 600 ))
    fi
    sleep 5
  done
  for f in summary.json requests.jsonl timeline.jsonl events.jsonl exit; do
    _lk exec "$LOADGEN_POD" -c loadgen -- cat "/out/$f" > "$dir/$f" 2>/dev/null || rm -f "$dir/$f"
  done
  _lk exec "$LOADGEN_POD" -c loadgen -- touch /out/collected >/dev/null 2>&1 || true
  _lk get pod "$LOADGEN_POD" -o json | jq '{pod: .metadata.name, node: .spec.nodeName, image: .spec.containers[0].image,
    image_id: .status.containerStatuses[0].imageID, resources: .spec.containers[0].resources}' > "$dir/loadgen-pod.json"
  _lk delete job "$LOADGEN_JOB" --wait=false >/dev/null 2>&1 || true
  LOADGEN_RC="$(cat "$dir/exit" 2>/dev/null || echo 1)"; rm -f "$dir/exit"
}
