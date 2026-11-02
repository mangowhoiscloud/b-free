T17 revert on executor 98f57c71 (front judge with TARGET_SHARE), Cloudflare LB b-free-chat.
revert/ (revert-fault.sh): dst app scaled to 0 eight seconds into the shift. step_10: 96 samples, by_env aws 92 /
  none 4, error rate 4.2 %, TARGET_SHARE onprem 0.000 < 0.025 -> REVERT_DST_0 CONFIRMED_APPLIED, run ABORT_PRE_WRITE,
  weights src 1 / dst 0. Same fault on bf05cadf gave a false DONE (../cloud-front-20261102T222627-t17-roundtrip).
error/ (error-fault.sh, 장애 주입 = spec S4 "목적지 앱 오류 응답"): dst pods answer 30 % of HTTP with 500
  (deploy/cloud/fault.sh, apps/demo PUT /admin/fault). step_10: 113 samples, by_env aws 103 / onprem 6 / none 4,
  error rate 3.5 % > 1 % -> REVERT_DST_0 CONFIRMED_APPLIED, ABORT_PRE_WRITE; fault cleared after (error-fault-off.jsonl).
Stage ms is now the step time (60.9 s, 60.6 s: change + 60 s hold).
