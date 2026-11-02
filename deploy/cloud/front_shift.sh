#!/usr/bin/env bash
# Front switch on its own (T17): executor-0 POST /v1/front/shift for b-free-chat, then wait for the run.
# LB mode walks 10 -> 50 -> 100 % with executor-0's own HTTPS probe judging each step (error rate, p95)
# and reverts the target pool to 0 % on a bad step. Never touches b-free (the platform; refused in code).
#   front_shift.sh <dst|src> [evidence-dir]
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
target="${1:?usage: front_shift.sh <dst|src> [evidence-dir]}"
EV="${2:-$RUNS/cloud-front-$(date +%Y%m%dT%H%M%S)-to-$target}"
mkdir -p "$EV"
executor_url
curl -fsS -m 90 "$EXECUTOR_URL/v1/front" > "$EV/front-before-to-$target.json"
code="$(curl -sS -o "$EV/shift-accepted-to-$target.json" -w '%{http_code}' -X POST "$EXECUTOR_URL/v1/front/shift" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: front-$target-$(uuidgen)" -d "{\"target_env\": \"$target\"}")"
[ "$code" = "202" ] || fail "POST /v1/front/shift -> $code: $(cat "$EV/shift-accepted-to-$target.json")"
id="$(jq -r .run_id "$EV/shift-accepted-to-$target.json")"
log "front shift to $target: run $id"
deadline=$(( $(date +%s) + 900 ))
while :; do
  curl -fsS "$EXECUTOR_URL/v1/runs/$id" > "$EV/shift-run-to-$target.json"
  [ "$(jq -r .status "$EV/shift-run-to-$target.json")" != "RUNNING" ] && break
  [ "$(date +%s)" -lt "$deadline" ] || fail "shift run $id still RUNNING after 900 s"
  sleep 2
done
curl -fsS -m 90 "$EXECUTOR_URL/v1/front" > "$EV/front-after-to-$target.json"
jq -c '{status, stages: [.stages[] | {name, ms, result, judgement: (.evidence.judgement // null)}]}' "$EV/shift-run-to-$target.json"
jq -c '{active_env, weights: .detail.pool_weights, probe}' "$EV/front-after-to-$target.json"
echo "$EV"
