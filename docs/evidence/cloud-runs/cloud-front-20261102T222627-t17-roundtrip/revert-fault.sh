#!/usr/bin/env bash
# Fault for the automatic revert (T17): shift to dst, and while the 10 % step is held, scale the on-prem app
# to 0 so the probes the LB sends to dst fail. Expect: judgement unhealthy, REVERT_DST_0, ABORT_PRE_WRITE.
set -u
EV="$1"; REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
( sleep 8; kubectl --context bravo-onprem -n chat scale deploy/chat-bravo-demo --replicas=0; date -u +%FT%TZ > "$EV/fault-at.txt" ) &
bash "$REPO/deploy/cloud/front_shift.sh" dst "$EV/revert" > "$EV/revert.log" 2>&1
kubectl --context bravo-onprem -n chat scale deploy/chat-bravo-demo --replicas=2
kubectl --context bravo-onprem -n chat rollout status deploy/chat-bravo-demo --timeout=180s
date -u +%FT%TZ > "$EV/restored-at.txt"
