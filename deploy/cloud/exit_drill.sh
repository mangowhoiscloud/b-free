#!/usr/bin/env bash
# T27 Exit Drill (after a DONE cutover): cut the destination app's path to the source DB (link
# port SRC_DB_APP_PORT only; the replication channel SRC_DB_REPL_PORT and the reverse relay stay),
# restart the destination app and PgBouncer, and check that new connections and writes work
# without the source. Records what the restarted PgBouncer selects by default.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
executor_url; URL="$EXECUTOR_URL"
OUT="$RUNS/cloud-t27-$(date +%Y%m%dT%H%M%S)"; mkdir -p "$OUT"
curl -fsS "$URL/v1/readiness" > "$OUT/readiness-before.json"
jq -e '.direction == "reverse"' "$OUT/readiness-before.json" >/dev/null || fail "not after a cutover (direction != reverse)"
log "cut: stop link src-db-app (destination app path to the source)"
kill "$(cat "$STATE/src-db-app.pid")" && rm -f "$STATE/src-db-app.pid"
echo "{\"cut\": \"src-db-app (127.0.0.1:${SRC_DB_APP_PORT})\", \"kept\": [\"src-db-repl\", \"reverse\"]}" > "$OUT/cut.json"
log "restart destination app and PgBouncer"
kubectl --context "$DST_CONTEXT" -n "$CHAT_NAMESPACE" rollout restart "$APP_WORKLOAD" deploy/chat-pgbouncer > "$OUT/restart.txt"
kubectl --context "$DST_CONTEXT" -n "$CHAT_NAMESPACE" rollout status deploy/chat-pgbouncer --timeout=180s >> "$OUT/restart.txt"
kubectl --context "$DST_CONTEXT" -n "$CHAT_NAMESPACE" rollout status "$APP_WORKLOAD" --timeout=180s >> "$OUT/restart.txt"
sleep 5
ops pool dst current > "$OUT/pgbouncer-after-restart.json"; jq -c . "$OUT/pgbouncer-after-restart.json"
"$REPO/apps/demo/.venv/bin/python" "$REPO/deploy/pilot/scripts/demo_traffic.py" \
  --url "wss://${FRONT_APP}-dst.mangowhois.ai/ws" --rate 5 --duration 15 --drain 20 > "$OUT/new-connections.json" || true
jq -c . "$OUT/new-connections.json"
curl -fsS "$URL/v1/readiness" > "$OUT/readiness-after.json"; jq -c '{direction, sub_state, lag_bytes}' "$OUT/readiness-after.json"
ok=1
jq -e '.active_side == "dst"' "$OUT/pgbouncer-after-restart.json" >/dev/null || { ok=0; log "restarted PgBouncer does not select dst by default (DONE commit of the default selection missing)"; }
jq -e '.unacked == 0 and .acked > 0' "$OUT/new-connections.json" >/dev/null || { ok=0; log "new connections/writes failed without the source"; }
jq -e '.direction == "reverse" and .sub_state == "streaming"' "$OUT/readiness-after.json" >/dev/null || { ok=0; log "replication channel did not stay up"; }
"$CLOUD_DIR/link.sh" up >/dev/null  # restore the app path link for later runs
[ "$ok" = "1" ] && log "PASS Exit Drill (evidence $OUT)" || fail "Exit Drill (evidence $OUT)"
