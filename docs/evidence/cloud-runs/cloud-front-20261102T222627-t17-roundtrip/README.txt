T17 front switch on Cloudflare LB b-free-chat (pools bravo-b-free-chat-src / -dst), started with
deploy/cloud/front_shift.sh through executor-0 POST /v1/front/shift. Judge: executor-0's own HTTPS probe of
https://b-free-chat.mangowhois.ai/api/config every 0.5 s for 60 s per step; healthy = >=30 samples,
error rate <= 1 %, p95 <= 800 ms. Raw run records: shift-run-to-*.json (stages, effects, judgement).
to dst (run 98d969c2): 10 % 103 samples err 0 p95 238 ms | 50 % 110 err 0 p95 117 ms | 100 % 114 err 0 p95 52 ms
to src             : 10 % 113 samples err 0 p95  70 ms | 50 % 114 err 0 p95  74 ms | 100 % 109 err 0 p95 178 ms
Stage "ms" shows 0 for steps on image bf05cadf (the stage is written after the hold); the fix (step_ms,
by_env) is in image f3f82550, not yet live (Argo CD tracks main).

Revert fault (revert-fault.sh, run in revert/): on-prem app scaled to 0 eight seconds into the shift to dst,
restored after the run. Result: run DONE, every step "healthy" (10 % 112 samples err 0 p95 106 ms; 50 % 100 err 0
p95 274 ms; 100 % 104 err 0 p95 234 ms), and the probe after the run answered "aws" with weights dst=1.
Cloudflare's pool monitor failed the dst pool over to src, so no user saw an error, and the judge (error rate,
p95 only) passed steps the target never served. Defect: a false DONE. Fix (front.judge): the target env id
must answer at least a quarter of its planned share (TARGET_SHARE), tests in tests/test_front.py. Not yet live.
