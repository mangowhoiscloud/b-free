#!/usr/bin/env bash
# T18 load run without a cutover: writes + reads through the customer app's public URL for a fixed time,
# then NFR-2 against one chat DB. The load runs as a Job in the platform cluster (load_lib.sh); this Mac
# only applies it and collects the files. Evidence: docs/evidence/load/<KST time>-<label>/.
#
#   deploy/cloud/load_run.sh [label]
#
# Env: APP_URL (https://<FRONT_APP>-src.mangowhois.ai), WRITE_RATE (50), READ_RATE (150), DURATION_S (300),
#      WORKERS (8), SAMPLE_CTX (source and platform: contexts whose node CPU is sampled), NFR2_SIDE (src),
#      PLAN_ID (optional, OTLP bravo.plan_id).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$CLOUD_DIR/load_lib.sh"
LABEL="${1:-baseline}"
APP_URL="${APP_URL:-https://${FRONT_APP}-src.mangowhois.ai}"
WRITE_RATE="${WRITE_RATE:-50}"; READ_RATE="${READ_RATE:-150}"; DURATION_S="${DURATION_S:-300}"
WORKERS="${WORKERS:-8}"; SAMPLE_CTX="${SAMPLE_CTX:-$SRC_CONTEXT $PLATFORM_CONTEXT}"; NFR2_SIDE="${NFR2_SIDE:-src}"
EV="$REPO/docs/evidence/load/$(TZ=Asia/Seoul date +%Y%m%d-%H%M%S)-$LABEL"
mkdir -p "$EV"
log "evidence: $EV"
(cd "$REPO/tools/loadgen" && uv sync --quiet --frozen)  # NFR-2 comparison runs here (no load)

# ---- context (no secrets) -------------------------------------------------------------------
jq -n --arg label "$LABEL" --arg url "$APP_URL" --arg kst "$(TZ=Asia/Seoul date -Iseconds)" \
  --arg git "$(git -C "$REPO" rev-parse HEAD)" --arg dirty "$(git -C "$REPO" status --porcelain -- tools/loadgen deploy/cloud | wc -l | tr -d ' ')" \
  --arg runner "Job in $PLATFORM_CONTEXT/$LOADGEN_NS ($(grep -m1 -o 'loadgen@sha256:[0-9a-f]*' "$LOADGEN_JOB_TMPL"))" \
  '{label: $label, app_url: $url, started_kst: $kst, git: $git, loadgen_or_scripts_uncommitted_paths: ($dirty | tonumber),
    runner: $runner}' > "$EV/context.json"
"$REPO/deploy/scripts/argo-state.sh" > "$EV/argo-state.txt" || true
for ctx in $SAMPLE_CTX; do
  [ "$ctx" = "$PLATFORM_CONTEXT" ] || kubectl --context "$ctx" -n "$CHAT_NAMESPACE" get deploy -o json | jq -c --arg ctx "$ctx" '{ctx: $ctx,
    deployments: [.items[] | {name: .metadata.name, replicas: .spec.replicas, image: .spec.template.spec.containers[0].image,
    resources: .spec.template.spec.containers[0].resources}]}' >> "$EV/workloads.jsonl"
  kubectl --context "$ctx" get nodes -o json | jq -c --arg ctx "$ctx" '.items[] | {ctx: $ctx, node: .metadata.name,
    type: .metadata.labels["node.kubernetes.io/instance-type"], cpu: .status.capacity.cpu,
    allocatable_cpu: .status.allocatable.cpu, memory: .status.capacity.memory}' >> "$EV/nodes.jsonl"
done

pids=()
cleanup() {
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done
  if [ -n "$LOADGEN_JOB" ]; then loadgen_job_stop; _lk delete job "$LOADGEN_JOB" --wait=false >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
for ctx in $SAMPLE_CTX; do "$CLOUD_DIR/load_sample.sh" "$ctx" "$EV/node-cpu-$ctx.jsonl" 5 & pids+=("$!"); done

# ---- load ---------------------------------------------------------------------------------------
RUN_ID="$(uuidgen | tr 'A-Z' 'a-z')"
loadgen_job_start "$RUN_ID" "$APP_URL" "$WRITE_RATE" "$READ_RATE" "$DURATION_S" "$WORKERS" "${PLAN_ID:-}"
loadgen_job_collect "$EV" $(( DURATION_S + 900 ))
lg_rc="$LOADGEN_RC"; LOADGEN_JOB=""
for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done; pids=()
jq -c '{writes: .writes | {sent, ok, failed, achieved_ok_per_s, latency_ms, attempt_errors},
        reads: .reads | {sent, ok, failed, achieved_ok_per_s, latency_ms, attempt_errors}, client}' "$EV/summary.json"

# ---- NFR-2 -------------------------------------------------------------------------------------
"$CLOUD_DIR/load_nfr2.sh" "$EV" "$NFR2_SIDE" && nfr2_rc=0 || nfr2_rc=$?
gzip -9 "$EV/requests.jsonl" "$EV/events.jsonl" "$EV/rows-$NFR2_SIDE.json"
jq -n --slurpfile s "$EV/summary.json" --slurpfile n "$EV/nfr2-$NFR2_SIDE.json" --argjson lg "$lg_rc" --argjson nr "$nfr2_rc" \
  '{loadgen_exit: $lg, nfr2_exit: $nr, writes_failed: $s[0].writes.failed, reads_failed: $s[0].reads.failed, nfr2_ok: $n[0].ok}' \
  > "$EV/verdict.json"
jq -c . "$EV/verdict.json"
log "done: $EV"
