# 배포된 Executor로 실제 쓰기 SHIFT(2026-11-03 04:05–04:07 KST, p95 기준 1,500 ms)

D-30(`3a6d168`) 뒤 executor-0이 `b2732bed`, `BRAVO_FRONT_MAX_P95_MS=1500`으로 뜬 것을 확인하고(04:04:58), `deploy/cloud/front_shift.sh dst`로 도구 경로 전환을 돌렸습니다. 10% 단계에서 목적지를 0%로 돌렸습니다(FAIL). 목적지는 기준 안이었습니다. 실패 사유는 어느 환경도 답하지 않은 오류 5건이고, 측정상 출발지 Gateway의 503으로 보입니다. LB는 출발지 100%입니다.

| 값 | 10% 단계(`front.step_10`) |
|---|---|
| 판정 원천 | `probe`(Prometheus의 부하 생성기 마지막 표본이 199초 전이라 활성 부하 없음) |
| 기준값(실행이 기록) | 목적지 표본 100건 이상, 오류율 1% 이하, p95 1,500 ms 이하, 보유 5–40초, 목적지 몫 1/4, 초당 60건, 새 연결 |
| 표본 | 641건: 출발지 527, 목적지 109, 환경 모름 5(모두 오류) |
| 목적지 | 표본 114(목적지 109 + 모름 5), 오류 5, 오류율 4.39%, p95 1,096.4 ms |
| 보유 · 단계 시간 | 11.3초 · 12.1초 |
| 판정 | `TARGET_ERROR_RATE(0.0439>0.01)` → `front.revert_0` |
| 앱 카운터(`chat_writes_total`) | 목적지 ok +109, 출발지 ok +527, 두 쪽 모두 오류 0 |

- 오류 5건은 앱까지 오지 않았습니다(앱 카운터 오류 0). `X-Served-By`가 없어 규칙대로 목적지 오류로 셌습니다.
- 바로 뒤(04:06) LB 출발지 100% 상태에서 같은 방식으로 600건을 보냈습니다. 18건(3%)이 Cloudflare를 거친 503이었고 본문은 `upstream connect error or disconnect/reset before headers. reset reason: connection timeout`(출발지 Cilium Gateway)이었습니다(`diag-src-burst.json`). 같은 시간대에 출발지 앱 복제본은 노드 `ip-10-10-11-11`과 `ip-10-10-11-107`에 하나씩 있었습니다. 부하 갈래의 `e25808e`(Gateway 정책 `remote-node` 허용) 뒤에도 연결 시간 초과가 3% 남았습니다.
- 그래서 출발지로 돌아오는 전환은 돌리지 않았습니다. 목적지 전환이 10%에서 복귀해 LB가 이미 출발지 100%이기 때문입니다.
- 실행 기록: `shift-run-to-dst.json`(단계, 효과, 판정, 기준값), `front-before-to-dst.json`, `front-after-to-dst.json`, `argo-state.txt`.
