# 논리 복제 타임아웃 60초 (2026-11-03 02:08 KST)

정방향 구독 `bravo_fwd_sub`(온프렘 → D-16 링크 → 출발지)가 몇 분마다 끊기고 다시 붙었습니다. 출발지 로그는 `terminating walsender process due to replication timeout`였습니다. 원인 설정은 CNPG 기본값 `wal_sender_timeout = 5s`, `wal_receiver_timeout = 5s`입니다. 컷오버 중 끊기면 barrier와 lag 확인이 늦어집니다.

## 바꾼 것

- 세 chat-db(출발지 EKS, 온프렘 RKE2, GKE)에 `wal_sender_timeout: "60s"`, `wal_receiver_timeout: "60s"`를 넣었습니다. 출발지는 정방향의 발행 쪽입니다. 온프렘은 정방향의 구독 쪽이고 failback 역방향의 발행 쪽입니다. GKE는 같은 판을 유지합니다.
- CNPG 1.30.1은 이 값을 막지 않습니다(server dry-run, 실제 적용 모두 받아들임). 둘 다 `sighup` 설정이라 재시작 없이 다시 읽었습니다(`pg_postmaster_start_time` 그대로, `pending_restart = f`, `apply.txt`).
- 파일: `deploy/landing-zone/overlays/eks/chat-db.yaml`, `deploy/landing-zone/base/chat-db.yaml`, `deploy/landing-zone/overlays/gke/manifests/30-chat-db.yaml`. 다른 landing-zone 변경과 같은 방식으로 live에 merge patch를 적용했고, 적용 뒤 렌더한 파일과 live 사이 `kubectl diff`는 비어 있습니다.

## 측정

| 구간 | 길이 | 끊김 | 근거 |
|---|---|---|---|
| 전(5초) | 16:39:35–17:07:48 UTC, 28분 | 3번(16:41:34, 16:47:22, 17:01:48). 약 9분마다 1번, 다시 붙는 데 0–1초 | `baseline-5s.txt` |
| 뒤(60초) | 17:08:17–17:30:45 UTC, 22분 | 0번. 온프렘 apply worker pid 1737, 출발지 walsender pid 1802(`backend_start` 17:01:47)가 끝까지 같음. `latest_end_time`은 30초 표본마다 앞으로 감 | `watch-60s.txt`, `logs-after-60s.txt` |

- 뒤 구간 서버 로그에 walsender 타임아웃, `could not receive data from WAL stream`, apply worker 재시작이 없습니다.
- 링크 클라이언트 `src-db-link`에 17:21:45 `failed to connect to origin error=EOF`가 1번 있습니다. 같은 시각 복제 연결의 pid는 바뀌지 않았으므로 다른 새 TCP 연결 하나가 실패한 것입니다.
- 끝난 뒤 Executor `/v1/readiness`는 `streaming`, lag 0입니다. 앱 끝점 세 곳 `/readyz`도 200입니다.

## 남는 점

- 링크가 정말 죽으면 양쪽이 알아차리는 데 최대 60초가 걸립니다. 컷오버 lag 확인은 `pg_stat_subscription`의 `latest_end_time`(지금은 30초 안에 갱신)을 함께 봐야 합니다.
- 끊김의 출발점은 측정으로 다 밝히지 못했습니다. 끊길 때 구독 쪽 EOF(16:41:32)가 walsender 타임아웃 로그(16:41:34)보다 2초 빨랐고 노드 시계 차이는 0.1초 안이었습니다. 그 시각 두 cloudflared 로그에는 아무것도 없습니다. 5초가 링크의 짧은 멈춤을 견디지 못한 것으로 보지만, 이 부분은 추정입니다.
