# 목적지 쓰기 경로 지연: 어디서 시간이 드나(2026-11-03 03:15–03:53 KST)

SHIFT 중 목적지 앱의 쓰기는 목적지 PgBouncer와 D-16 링크(`src-db-link`, `cloudflared access tcp`)를 거쳐 출발지 주 DB에 갑니다. 쓰기 한 건이 2.0–2.6초였던 까닭은 연결을 새로 여는 비용이 아니라 왕복 횟수였습니다. 한 왕복이 약 340 ms였고, 쓰기 한 건이 6왕복이었습니다.

| 측정 | 값 | 파일 |
|---|---|---|
| 온프렘 앱 Pod: PgBouncer 연결 열기 | 11 ms(서버 연결은 이미 열려 재사용) | `measurements.jsonl` |
| 온프렘 앱 Pod: `SELECT 1` 한 왕복 | p50 339.9 ms | `measurements.jsonl` |
| 온프렘 앱 Pod: 쓰기 트랜잭션 6왕복(BEGIN, set_config, 차단 확인, INSERT, SELECT, COMMIT) | p50 2,047 ms | `measurements.jsonl` |
| 같은 문장을 한 번의 simple-query로 | p50 340.3 ms | `measurements.jsonl`, `rtt-onprem-after.json` |
| 출발지 앱 Pod: 같은 셋 | 0.5, 5.2, 2.5 ms | `measurements.jsonl` |
| 플랫폼에서 같은 종류의 링크로 두 DB | `SELECT 1` 8.6, 10.2 ms | `measurements.jsonl` |
| 이 망에서 `t1-src-db.mangowhois.ai`의 Cloudflare POP | DFW(같은 망에서 `www.cloudflare.com`은 ICN) | `measurements.jsonl` |
| 앱 0.3.2(1왕복) 뒤 플랫폼에서 `b-free-chat-dst`로 차례 쓰기 20건 | p50 698, p95 705 ms | `direct-writes-after-fix.json` |
| PgBouncer 풀 30 · 최소 10 뒤 같은 측정 | p50 598, p95 859 ms | `../lb-real-writes-20261103T033356/README.md` |

- 고친 것: 앱 `a4636ac`(첨부 없는 쓰기는 한 번의 simple-query 배치, 쓰기 차단은 FOR SHARE 그대로), PgBouncer `default_pool_size` 30, `min_pool_size` 10(`deploy/landing-zone/base/chat-pgbouncer.yaml`).
- 배포: 앱 0.3.2(`86670b4d`)를 03:27–03:30에 세 클러스터에 올렸습니다(`rollout.txt`). 온프렘 첫 시도는 NetworkPolicy 필드 소유 충돌로 실패했습니다. 두 번째 시도는 release의 옛 값으로 RGW egress 포트를 8080 · 8443 · 80까지 넓혔고, 03:28:13에 커밋된 값 파일로 다시 올려 443만 남겼습니다(약 18초). 그 뒤 부하 갈래가 0.3.3(`76e8f595`)을 올렸습니다.
- 남은 바닥: 온프렘 링크의 한 왕복 290–340 ms는 이 망에서 Cloudflare 경로가 미국 POP를 지나기 때문으로 보입니다(POP 관측은 했고, 경로 전체는 재지 않음). 컷오버 뒤에는 목적지 앱이 로컬 DB에 쓰므로 이 지연은 SHIFT 동안만 있습니다.
