#!/usr/bin/env bash
# T27 / FR-15 Exit Drill on the current environments (D-16 links, 2026-11-03).
#
# Spec: cut the destination app's access to the source, keep the replication channel, then show that a
# restarted destination app takes new connections and writes. The source here is source EKS; the
# destination is on-prem RKE2 (DST_CONTEXT).
#
#   deploy/cloud/exit_drill.sh
#
# 1. Preflight (always, read-only): executor-0 readiness and the last cutover run.
# 2. Residual dependency map (always, read-only): every path from the destination app and its DB to
#    the outside, measured from the cluster (PgBouncer active and restart-default target, subscriptions,
#    link clients, app settings, images, egress policies, secret stores, DB backup target).
# 3. The cut (only when executor-0 says direction=reverse and the last cutover is DONE):
#    - scale the on-prem link client src-db-link (t1-src-db) to 0 and prove the path is closed from an
#      app Pod. The reverse replication channel (source subscribes through its own dst-db-link) stays.
#    - restart the destination app. PgBouncer is restarted only if its restart default selects the
#      destination; otherwise the step is FAIL with the measured reason (a restart would point the app
#      at the cut source and take the active environment down).
#    - new connections and writes through https://<FRONT_APP>-dst.mangowhois.ai (tools/loadgen app mode as a
#      platform Job, never from this Mac; NFR-2 on the destination DB), then readiness: still reverse and streaming.
#    - restore src-db-link to its replica count.
# Not after a cutover: steps 1-2 run, the verdict is NOT_RUN with the measured reason, exit 2.
# Evidence: docs/evidence/exit-drill/<KST time>/ (no secrets: DSN passwords are never read or printed).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$CLOUD_DIR/load_lib.sh"
executor_url; URL="$EXECUTOR_URL"
EV="$REPO/docs/evidence/exit-drill/$(TZ=Asia/Seoul date +%Y%m%d-%H%M%S)"
mkdir -p "$EV"
log "evidence: $EV"
D=(kubectl --context "$DST_CONTEXT" -n "$CHAT_NAMESPACE")
S=(kubectl --context "$SRC_CONTEXT" -n "$CHAT_NAMESPACE")
LINK=deploy/src-db-link
restore_replicas=""
restore_link() {
  if [ -n "$restore_replicas" ]; then
    "${D[@]}" scale "$LINK" --replicas="$restore_replicas" >/dev/null && \
      "${D[@]}" rollout status "$LINK" --timeout=180s >/dev/null && log "restored $LINK to $restore_replicas"
    restore_replicas=""
  fi
}
trap 'restore_link; [ -z "$LOADGEN_JOB" ] || _lk delete job "$LOADGEN_JOB" --wait=false >/dev/null 2>&1 || true
      [ -z "$EXECUTOR_PF_PID" ] || kill "$EXECUTOR_PF_PID" 2>/dev/null || true' EXIT

jq -n --arg git "$(git -C "$REPO" rev-parse HEAD)" --arg kst "$(TZ=Asia/Seoul date -Iseconds)" \
  --arg src "$SRC_CONTEXT" --arg dst "$DST_CONTEXT" '{started_kst: $kst, git: $git, src: $src, dst: $dst}' > "$EV/context.json"
"$REPO/deploy/scripts/argo-state.sh" > "$EV/argo-state.txt" || true

# ---- 1. preflight ------------------------------------------------------------------------------
curl -fsS "$URL/v1/readiness" > "$EV/readiness-before.json"
curl -fsS "$URL/v1/runs?kind=cutover&limit=1" | jq '.[0] // null' > "$EV/last-cutover.json"
jq -c '{direction, sub_state, lag_bytes}' "$EV/readiness-before.json"

# ---- 2. residual dependency map -------------------------------------------------------------------
pgb_pod="$("${D[@]}" get pod -l app.kubernetes.io/name=chat-pgbouncer -o jsonpath='{.items[0].metadata.name}')"
active="$("${D[@]}" exec "$pgb_pod" -c pgbouncer -- cat /run/pgbouncer/active.ini | sed -n 's/^chat = //p')"
default_ini="$("${D[@]}" get deploy chat-pgbouncer -o json \
  | jq -r '[.spec.template.spec.initContainers[]?.command[]? | (capture("cp /etc/pgbouncer/targets/(?<f>target-[a-z]+\\.ini)") | .f)?] | first // "unknown"')"
default_target="$("${D[@]}" get configmap chat-pgbouncer-targets -o json | jq -r --arg f "$default_ini" '.data[$f] // ""' | sed -n 's/^chat = //p')"
primary() { "$@" get pod -l cnpg.io/cluster=chat-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}'; }
dst_subs="$("${D[@]}" exec "$(primary "${D[@]}")" -c postgres -- psql -X -A -t -F '|' -d chat -c \
  "select subname, subenabled, substring(subconninfo from 'host=([^ ]+)') from pg_subscription" 2>/dev/null || true)"
src_subs="$("${S[@]}" exec "$(primary "${S[@]}")" -c postgres -- psql -X -A -t -F '|' -d chat -c \
  "select subname, subenabled, substring(subconninfo from 'host=([^ ]+)') from pg_subscription" 2>/dev/null || true)"
app_dsn_host="$("${D[@]}" get secret bravo-demo-db -o jsonpath='{.data.dsn}' | base64 -d \
  | python3 -I -c 'import re,sys; m=re.search(r"host=(\S+)", sys.stdin.read()); print(m.group(1) if m else "unparsed")')"
app_env="$("${D[@]}" get deploy "${APP_WORKLOAD#deploy/}" -o json | jq '[.spec.template.spec.containers[0].env[]
  | select(.value != null) | select(.name | test("^(S3_ENDPOINT_URL|S3_BUCKET|DB_PASSWORD_SOURCE|OTEL_EXPORTER_OTLP_ENDPOINT|AWS_.*|APP_PUBLIC_URL)$"))
  | {(.name): .value}] | add')"
links="$("${D[@]}" get deploy -l app.kubernetes.io/part-of=bravo-link -o json | jq '[.items[] | {name: .metadata.name,
  replicas: .spec.replicas, hostname: ([.spec.template.spec.containers[0].args[]] | (index("--hostname")) as $i | if $i then .[$i+1] else null end)}]')"
images="$("${D[@]}" get pods -o json | jq '[.items[].spec.containers[].image | split("/")[0]] | unique')"
egress="$("${D[@]}" get networkpolicy -o json | jq '[.items[] | select(.spec.policyTypes | index("Egress")) | {name: .metadata.name,
  pods: .spec.podSelector.matchLabels, to: [.spec.egress[]?.to[]? | (.podSelector.matchLabels // .ipBlock.cidr // .namespaceSelector.matchLabels)]}]')"
stores="$(kubectl --context "$DST_CONTEXT" get clustersecretstores,secretstores -A -o json 2>/dev/null \
  | jq '[.items[] | {kind, name: .metadata.name, provider: (.spec.provider | keys)}]' || echo '[]')"
backup="$("${D[@]}" get cluster.postgresql.cnpg.io chat-db -o json | jq '.spec.backup.barmanObjectStore | {destinationPath, endpointURL}')"
jq -n --arg active "$active" --arg default_ini "$default_ini" --arg default_target "$default_target" \
  --arg dst_subs "$dst_subs" --arg src_subs "$src_subs" --arg app_dsn_host "$app_dsn_host" \
  --argjson app_env "$app_env" --argjson links "$links" --argjson images "$images" --argjson egress "$egress" \
  --argjson stores "$stores" --argjson backup "$backup" '
  def side(h): if (h | test("src-db-link|amazonaws|t1-src-")) then "source-aws"
    elif (h | test("chat-db-rw|rook-ceph|bravo\\.local|bravo-observability")) then "local" else "other" end;
  def subs(s): [s | split("\n")[] | select(length > 0) | split("|") | {name: .[0], enabled: (.[1] == "t"), host: .[2]}];
  {
    pgbouncer: {active: $active, active_points_to: side($active),
                restart_default_file: $default_ini, restart_default: $default_target,
                restart_default_points_to: side($default_target)},
    app: {db_dsn_host: $app_dsn_host, settings: $app_env, images_registries: $images},
    subscriptions: {on_destination_db: subs($dst_subs), on_source_db: subs($src_subs)},
    link_clients_in_destination: [$links[] | . + {points_to: side(.hostname // "")}],
    egress_policies: $egress, secret_stores: $stores, db_backup: $backup
  }' > "$EV/dependency-map.json"
jq -c '{pgbouncer, app: .app.db_dsn_host, subs: .subscriptions, links: [.link_clients_in_destination[] | {name, points_to}]}' "$EV/dependency-map.json"

verdict() {  # verdict <status> <reason>
  jq -n --arg st "$1" --arg why "$2" --slurpfile r "$EV/readiness-before.json" --slurpfile c "$EV/last-cutover.json" \
    --slurpfile m "$EV/dependency-map.json" '{status: $st, reason: $why,
      readiness_before: ($r[0] | {direction, sub_state, lag_bytes}),
      last_cutover: ($c[0] | if . then {run_id, status, started_at} else null end),
      pgbouncer_restart_default_points_to: $m[0].pgbouncer.restart_default_points_to}' > "$EV/verdict.json"
}
direction="$(jq -r .direction "$EV/readiness-before.json")"
last_status="$(jq -r '.status // "none"' "$EV/last-cutover.json")"
if [ "$direction" != "reverse" ] || [ "$last_status" != "DONE" ]; then
  verdict NOT_RUN "not after a DONE cutover: readiness direction=$direction, last cutover status=$last_status; the cut needs the S3 cutover (human approval) first"
  jq -c . "$EV/verdict.json"
  log "NOT_RUN (preflight and dependency map recorded in $EV)"
  exit 2
fi

# ---- 3. the cut ----------------------------------------------------------------------------------
restore_replicas="$("${D[@]}" get "$LINK" -o jsonpath='{.spec.replicas}')"
log "cut: scale $LINK (t1-src-db) to 0 (was $restore_replicas); the source's dst-db-link and bravo_rev_sub stay"
"${D[@]}" scale "$LINK" --replicas=0 > "$EV/cut.txt"
"${D[@]}" wait --for=delete pod -l app.kubernetes.io/name=src-db-link --timeout=120s >> "$EV/cut.txt" 2>&1 || true
app_pod="$("${D[@]}" get pod -l app.kubernetes.io/name=bravo-demo -o jsonpath='{.items[0].metadata.name}')"
"${D[@]}" exec "$app_pod" -c app -- python -c '
import json, socket
try:
    socket.create_connection(("src-db-link.chat.svc.cluster.local", 5432), 3).close(); print(json.dumps({"src_db_link_reachable": True}))
except OSError as e:
    print(json.dumps({"src_db_link_reachable": False, "error": type(e).__name__}))' > "$EV/cut-probe.json"
jq -c . "$EV/cut-probe.json"

log "restart the destination app"
"${D[@]}" rollout restart "$APP_WORKLOAD" > "$EV/restart.txt"
"${D[@]}" rollout status "$APP_WORKLOAD" --timeout=300s >> "$EV/restart.txt"
pgb_restart="skipped"
if [ "$(jq -r .pgbouncer.restart_default_points_to "$EV/dependency-map.json")" = "local" ]; then
  "${D[@]}" rollout restart deploy/chat-pgbouncer >> "$EV/restart.txt"
  "${D[@]}" rollout status deploy/chat-pgbouncer --timeout=180s >> "$EV/restart.txt"
  pgb_restart="restarted"
  sleep 5
else
  log "PgBouncer not restarted: its restart default ($(jq -r .pgbouncer.restart_default_file "$EV/dependency-map.json")) points to the source"
fi
pgb_pod="$("${D[@]}" get pod -l app.kubernetes.io/name=chat-pgbouncer -o jsonpath='{.items[0].metadata.name}')"
"${D[@]}" exec "$pgb_pod" -c pgbouncer -- cat /run/pgbouncer/active.ini | sed -n 's/^chat = //p' > "$EV/pgbouncer-active-after.txt"

log "new connections and writes through https://${FRONT_APP}-dst.mangowhois.ai without the source"
mkdir -p "$EV/new-connections"
(cd "$REPO/tools/loadgen" && uv sync --quiet --frozen)  # NFR-2 comparison only; the load is a platform Job
loadgen_job_start "$(uuidgen | tr 'A-Z' 'a-z')" "https://${FRONT_APP}-dst.mangowhois.ai" 5 15 20 1
loadgen_job_collect "$EV/new-connections" 600; LOADGEN_JOB=""
"$CLOUD_DIR/load_nfr2.sh" "$EV/new-connections" dst > /dev/null 2>&1 || true
curl -fsS "$URL/v1/readiness" > "$EV/readiness-after.json"
restore_link

ok=1; why=()
jq -e '.src_db_link_reachable == false' "$EV/cut-probe.json" >/dev/null || { ok=0; why+=("source path still reachable after the cut"); }
[ "$pgb_restart" = "restarted" ] || { ok=0; why+=("PgBouncer restart default points to the source (init container copies $(jq -r .pgbouncer.restart_default_file "$EV/dependency-map.json"))"); }
grep -q "chat-db-rw" "$EV/pgbouncer-active-after.txt" || { ok=0; why+=("PgBouncer active target after the cut is not the destination DB"); }
jq -e '.writes.ok > 0 and .writes.failed == 0 and .reads.failed == 0 and (.writes.served_by | keys == ["onprem"])' \
  "$EV/new-connections/summary.json" >/dev/null || { ok=0; why+=("new connections or writes failed without the source"); }
jq -e '.ok == true' "$EV/new-connections/nfr2-dst.json" >/dev/null 2>&1 || { ok=0; why+=("NFR-2 on the destination DB failed"); }
jq -e '.direction == "reverse" and .sub_state == "streaming"' "$EV/readiness-after.json" >/dev/null \
  || { ok=0; why+=("replication channel did not stay up"); }
reason="$(printf '%s; ' ${why[@]+"${why[@]}"})"
[ "$reason" != "; " ] || reason=""
verdict "$([ "$ok" = 1 ] && echo PASS || echo FAIL)" "${reason:-all checks passed}"
jq -c . "$EV/verdict.json"
[ "$ok" = "1" ] && log "PASS Exit Drill ($EV)" || fail "Exit Drill ($EV)"
