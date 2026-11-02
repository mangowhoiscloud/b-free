#!/usr/bin/env bash
# 장애 주입 at the destination app (spec S4 "목적지 앱 오류 응답"): dst pods answer 30 % of HTTP requests with
# 500 (health paths excluded, so Cloudflare keeps the pool), then a shift to dst. Expect ERROR_RATE -> revert.
set -u
EV="$1"; REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
bash "$REPO/deploy/cloud/fault.sh" dst 0.3 > "$EV/error-fault-on.jsonl"
bash "$REPO/deploy/cloud/front_shift.sh" dst "$EV/error" > "$EV/error.log" 2>&1
bash "$REPO/deploy/cloud/fault.sh" dst 0 > "$EV/error-fault-off.jsonl"
