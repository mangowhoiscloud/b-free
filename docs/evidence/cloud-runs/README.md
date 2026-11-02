# 클라우드 실행 기록 (2026-11-02 20:49–23:02 KST)

클라우드 E2E 스크립트(`deploy/cloud/`)가 Git 제외 폴더 `deploy/pilot/runs/cloud-*`에 남긴 기록을 옮겼습니다. 성공과 실패를 모두 옮깁니다(감사 B06 · B22, 리뷰 P4). 폴더 이름의 시각은 KST이고, 파일 안의 시각은 UTC입니다. 아래 표의 시각은 모두 KST입니다.

색인을 만들면서 클러스터에 쓰는 명령은 돌리지 않았습니다. 읽기 전용 관측 하나를 더했습니다([platform-snapshot-20261103T0232KST.txt](platform-snapshot-20261103T0232KST.txt)).

## 판정 규칙

- 성공은 기록 안의 상태 값으로만 셉니다. `DONE`, `complete: true`, `streaming`이 성공입니다.
- `NOT_STARTED`, 빈 `summary.json`, `skipped`는 성공이 아닙니다.
- 스크립트가 Executor API를 직접 부른 실행은 "하네스 실행"입니다. 콘솔 · 승인 · 상태 머신을 거친 실행이 아닙니다.

## 플랫폼 DB가 말하는 것 (2026-11-03 02:32 KST)

플랫폼 DB `bravo`를 읽었습니다(`platform-snapshot-20261103T0232KST.txt`).

| 표 | 행 수 | 뜻 |
|---|---|---|
| `plans` | 0 | 플랫폼 API로 계획을 만든 적이 없습니다. S1–S4를 콘솔 · API로 돈 적이 없습니다 |
| `approvals` | 0 | 사람 승인(change · cutover · failback)이 한 건도 없습니다 |
| `control_exceptions` | 0 | S1 5b 예외 승인도 없습니다. 마이그레이션 0008은 적용돼 있습니다 |
| `changes`, `rehearsals`, `gate_verdicts`, `environments` | 0 | 리허설 · 변경 · 게이트 판정이 API 경로로 저장된 적이 없습니다 |
| `receipts` | 0 | 컷오버 · 되돌리기 영수증이 없습니다 |
| `runs` | `shift` 6(DONE 4, ABORT_PRE_WRITE 2) | 아래 앞단 전환 6회와 같습니다. `cutover` · `failback` run은 0입니다 |
| `principals` / `memberships` | 1 / approver 1, viewer 1 | 로그인한 신원은 approver(gmail) 하나입니다. operator(naver)는 첫 로그인 전이라 역할이 없습니다 |
| `attachment_copies` | 3 | 아래 T8 복사 3건입니다 |

## 색인

| 폴더 | 시각(KST) | 시나리오 | 결과 | 증명하는 것 | 증명하지 않는 것 |
|---|---|---|---|---|---|
| [cloud-front-20261102T222627-t17-roundtrip](cloud-front-20261102T222627-t17-roundtrip/README.txt) (폴더 바로 아래, run `98d969c2`) | 22:26:28–22:29:33 (184.8초) | T17 앞단 전환 src → dst. Cloudflare LB `b-free-chat`, Executor 이미지 `bf05cadf`, `deploy/cloud/front_shift.sh`가 executor-0 `POST /v1/front/shift`를 부름 | **DONE.** 10 · 50 · 100% 모두 `CONFIRMED_APPLIED`(LB에서 다시 읽은 가중치가 같음). 표본 103 · 110 · 114, 오류 0, p95 238 · 117 · 52 ms. 끝난 뒤 프로브 `env.id=onprem` | LB 가중치가 Executor API로 실제로 10 → 50 → 100%로 바뀜. 편도 시간 약 185초(단계마다 60초 보유) | 콘솔 시작 · 승인 · 상태 머신 SHIFT(스크립트가 직접 부름, `plan_id`는 합성 값). 업무 트래픽 품질(판정 표본은 Executor 자신의 `GET /api/config` 프로브, 감사 B14). 이 이미지는 응답 환경별 수(`by_env`)를 남기지 않아 단계마다 dst가 실제로 응답했는지 모름(아래 거짓 DONE 참고) |
| 같은 폴더 (run `41fe9064`) | 22:35:43–22:38:49 (185.3초) | dst → src 같은 방식 | **DONE.** 표본 113 · 114 · 109, 오류 0, p95 70 · 74 · 178 ms. 끝난 뒤 `env.id=aws` | 반대 방향 이동 | 위와 같음 |
| 같은 폴더 `revert/` (run `41aa9080`) | 22:39:12–22:42:17 (184.7초). 장애 22:39:19, 복구 22:42:24 | 자동 0% 복귀 시험: 전환 8초 뒤 온프렘 앱 replica 0 | **거짓 DONE(결함).** 세 단계 모두 "정상"(표본 112 · 100 · 104, 오류 0). 끝난 뒤 가중치 dst=1인데 프로브 응답은 `aws` | Cloudflare pool 모니터가 죽은 dst를 src로 넘겨 판정이 속음. 이 결함 때문에 `TARGET_SHARE` 판정을 넣음(`front.judge`, `tests/test_front.py`) | 복귀 동작 |
| 같은 폴더 `back-to-src/` (run `9cb1a416`) | 22:43:12–22:46:17 (185.1초) | 시험 뒤 src로 되돌림 | **DONE.** 표본 115 · 116 · 115, 오류 0, p95 33 · 23 · 33 ms | 원래 상태로 돌아옴 | 위와 같음 |
| [cloud-front-20261102T225316-t17-revert-98f57c71](cloud-front-20261102T225316-t17-revert-98f57c71/README.txt) `revert/` (run `dd2e674c`) | 22:53:18–22:54:22 (63.9초). 장애 22:53:24, 복구 22:54:29 | 같은 장애(온프렘 앱 replica 0)를 Executor `98f57c71`(`TARGET_SHARE` 판정)로 | **ABORT_PRE_WRITE**(기대한 중단). 10% 단계 표본 96, `by_env` aws 92 · 없음 4, 오류율 4.2%, `TARGET_SHARE(onprem:0.000<0.025)` → `REVERT_DST_0` `CONFIRMED_APPLIED`, 가중치 src 1 / dst 0 | dst가 응답하지 않으면 10% 단계에서 0%로 자동 복귀(S3 예외 1a의 앞단 부분) | 콘솔 시작. 실제 채팅 ACK · 쓰기 오류 기준 판정(B14). p95 초과 조건. 쓰기 경로가 있는 컷오버 안의 복귀 |
| 같은 폴더 `error/` (run `76d37a38`) | 22:54:40–22:55:43 (63.2초). 장애 22:54:38–22:55:47 | 장애 주입(스펙 S4 "목적지 앱 오류 응답"): 온프렘 앱 Pod 2개가 HTTP의 30%를 500으로(헬스 경로 제외), `deploy/cloud/fault.sh` | **ABORT_PRE_WRITE**(기대한 중단). 10% 단계 표본 113, `by_env` aws 103 · onprem 6 · 없음 4, 오류율 3.5% > 1% → `REVERT_DST_0` | 오류율 기준의 자동 복귀 | 표본이 작음(dst 응답 6개). p95 기준. 업무 트래픽 기준 |
| [cloud-t8-attachments-20261102T225707](cloud-t8-attachments-20261102T225707/) | 업로드 22:57:18, 복사 22:57:49 · 22:57:53, 내려받기 확인 22:58:16, CLI 재실행 23:01:15 | T8 기존 첨부 동기화: 출발지 앱으로 첨부 3개(32 · 64 · 96 KiB, 메시지 5–7) 업로드 → executor-0 안에서 S3 → 온프렘 RGW 복사 | **complete.** 1회차 참조 3, 복사 3, 실패 0, 2,626 ms. 2회차 이미 검증 3, 복사 0, 850 ms. 각 첨부를 `b-free-chat-src` · `b-free-chat-dst`로 내려받아 HTTP 200, SHA-256 일치. 새 CLI(Executor `f5ec7dfd`)로 다시 돌려도 이미 검증 3 | 복사와 SHA-256 대조, 목적지 앱이 RGW에서 읽음, 다시 돌려도 중복 없음 | 동결 · watermark, 컷오버 run 안의 복사, 영수증 `objects[]`(B28), WATCH 역복사, 컷오버 시점의 최종 첨부 집합, 규모(3개, 192 KiB). 1 · 2회차는 이미지 `98f57c71`에 같은 논리 스크립트를 파이프로 넣어 돌림(`copy_in_executor0.py`) |
| [cloud-20261102T225937-cutover](cloud-20261102T225937-cutover/) | 22:59:37–23:00:11 | 하네스 dry run(`DRY-RUN.txt`: `SKIP_FRONT=1`, `FAULT_RATE=0`, 짧은 `WAIT_RUN_S`). 콘솔에서 시작한 컷오버가 없음 | **컷오버 없음. 기록도 불완전**(`summary.json` 0바이트, `cutover-run.json` 없음). 부하 518건 보냄 = 518건 확인, 실패 0, p50 12.8 · p99 133.4 ms, NFR-2 유실 0 · 중복 0. 채팅 WS 52/52(`aws`). 복제 forward `streaming`, lag 0, 스키마 해시 같음, 행 7 → 577 양쪽 같음 | 출발지 PgBouncer로 가는 부하 경로와 NFR-2 대조가 클라우드에서 동작. 초당 20건(`cloud.env` `LOADGEN_RATE`) 동안 정방향 복제가 따라옴 | 컷오버, 쓰기 정지, 영수증, 되돌리기, 앞단 전환. 50 req/s(스펙 5.3). 그때 Executor 통제 확인은 `DB-AUTH` 양쪽 FAIL |
| [cloud-20261102T230116-cutover](cloud-20261102T230116-cutover/) | 23:01:16–23:01:53 | 같은 dry run | **실패: 컷오버 `NOT_STARTED`, 되돌리기 `skipped`, `ok: false`.** 부하 407/407, p99 86.5 ms, NFR-2 0 · 0. 채팅 WS 37/37, 최대 지연 1,540.8 ms. 행 577 → 1,021 양쪽 같음 | 위와 같음 | 위와 같음. 이 실행을 컷오버 성공으로 인용하지 않습니다 |
| [cloud-executor0-checks-20261102T221434](cloud-executor0-checks-20261102T221434/) | 22:14–22:25 | executor-0(플랫폼)을 D-16 링크 topology로 바꾼 뒤 점검 | **E4 통과**: observe · stats 자격으로 fence 갱신, barrier 쓰기, 역할 변경, 구독, 첨부 동결, PgBouncer PAUSE · RELOAD가 두 쪽 모두 거부(`all_refused: true`). 온프렘 PgBouncer의 출발지 대상이 22:19에 `host.lima.internal:15434`(임시 링크)에서 `src-db-link`(D-16)로 바뀜. 그 경로로 온프렘 앱 쓰기 HTTP 201(22:20:43, 노트북 링크를 내린 뒤 22:21:31). 출발지 앞단 503의 원인(관리 커넥터용 NetworkPolicy가 앱 Pod를 골라 Gateway 허용이 빠짐)과 고침(`chat-app-from-gateway`), 고친 뒤 200 | E4 계약, 노트북 없이 D-16 링크로 도는 데이터 경로 | E1 · E5 · E6(사람 승인 필요). 22:14 시점 readiness와 dst 통제는 `unknown` · `UNKNOWN` |
| [cloud-repl-eks-rke2-20261102T221751](cloud-repl-eks-rke2-20261102T221751/) | 22:17:53–22:18:36 | 출발지 EKS → 온프렘 RKE2 정방향 복제를 D-16 링크로 다시 세움(rearm → setup) | rearm은 정방향 streaming(22:18:17)까지 간 뒤 **실패 42501**(출발지 권한 없음). 출발지 역할 부여(`db_roles-source-grant.out`) 뒤 setup **성공**: 정방향 streaming, 역방향 구독 생성(꺼짐), 행 2 = 2 | D-16 링크 위 논리 복제 | 복제 끊김 내성(이후 `../repl-timeouts/` 참고), 부하 중 lag |
| [cloud-dbroles-onprem-rke2-20261102T221719](cloud-dbroles-onprem-rke2-20261102T221719/) | 22:17:19 | 온프렘 RKE2 chat-db에 스키마 01 · 02 · 03_dst와 역할 | **성공.** 역할 `executor_ctl` · `executor_observe` · `replicator` 설정, 비밀번호는 SCRAM 검증값(`app_writer:scram`) | 역할과 SCRAM 저장(감사 B19의 일부) | 역할 비밀번호의 회전 |
| [laptop-pause-experiment-20261102T220826](laptop-pause-experiment-20261102T220826/) (원래 이름 `cloud-pause-experiment-…`) | 22:08 | **노트북 Docker 실험. 클라우드 아님.** PostgreSQL 16.15 + `bravo-pgbouncer:pilot`, 기본 `pre_auth_delay=1`(느린 서버 로그인 흉내), PgBouncer `PAUSE` · `RESUME` 반복 | 16회. `PAUSE` 명령 자체 1.3–8.8 ms. 클라이언트 최대 지연: single 1.3–2.6초, rapid 1.3–3.2초, midlogin 7.7초, spike 26–28 ms(처음 두 번) ~ 3.1–7.85초. spike 일부에서 `server login has been failing` 재시도 88–444회, 한 번은 요청 실패 6건 | PAUSE 명령은 ms 단위지만, 서버 로그인 중에 걸리면 클라이언트가 초 단위로 기다릴 수 있음 | 클라우드 값. 컷오버 쓰기 정지 값. 실행마다 `SLOW` · `MINPOOL` · `TLS` 값이 결과 줄에 다 남지 않아 조건별 비교는 못 함 |
| [history-k3s-20261102T2049-2101](history-k3s-20261102T2049-2101/) | 20:49–21:07 | **역사 기록.** 21:10 RKE2 결정(ADR 0010) 전, 온프렘 K3s · 플랫폼 EKS · 노트북 임시 링크(port-forward) · 노트북 OpenBao 포워드 시절 | `db_roles` 실패 2(20:49 OpenBao 포워드 연결 거부, 20:50 OpenBao TLS `Missing Authority Key Identifier`) 뒤 성공 1(20:51). `bootstrap` 20:53 "연결 안 됨" 3(출발지 chat-db · chat-admin, 역방향 relay) → 21:07 모두 ok. 복제 setup 실패 2(20:56 `127.0.0.1:15432` 거부, 20:57 연결 끊김) 뒤 21:00 · 21:01 정방향 streaming. 21:01 점검: LB 읽기(src 1 / dst 0), K3s PgBouncer 출발지 대상 `host.lima.internal`, 역방향 경로(EKS Pod → relay → 노트북 에이전트 → K3s) SSLRequest 5/5 응답 23.6–30.6 ms | 그때의 경로가 실제로 돌았다는 것 | 지금 경로. 이 폴더의 명령과 포트는 지금 실행 지침이 아닙니다 |

## 요약

| 항목 | 클라우드 실측 | 횟수 |
|---|---|---|
| LB 가중치 10 / 50 / 100% 이동 | 있음(하네스) | 4회 DONE. 그중 1회는 거짓 DONE(결함, 고침) |
| 앞단 자동 0% 복귀 | 있음(하네스) | 2회 ABORT_PRE_WRITE(63초) |
| 첨부 S3 → RGW 복사 · 대조 | 있음(단독 실행) | 1회(3개) + 재실행 2회 |
| E4(관찰 자격의 쓰기 거부) | 있음 | 두 쪽 1회 |
| D-16 링크 위 정방향 복제 | 있음 | 1회 세움 |
| 클라우드 컷오버 | **없음** | 0회(dry run 2회는 `NOT_STARTED` · 불완전) |
| 클라우드 되돌리기 | **없음** | 0회 |
| 영수증 | **없음** | 0건 |
| 콘솔에서 시작한 실행 · 사람 승인 | **없음** | 0건 |
| 50 req/s 부하, 읽기 부하 | **없음** | 0회(초당 20건 쓰기만) |
| Exit Drill(T27) | **없음** | 0회 |

## 옮기면서 바꾼 것

- 저장소 절대 경로를 `<repo>`로 바꿨습니다.
- `cloud-executor0-checks-…/onprem-write-via-d16.txt`의 멱등 키 2개는 앞 8자만 남겼습니다. 비밀은 아니지만 gitleaks가 API 키 모양으로 잡습니다.
- PAUSE 실험의 로컬 Docker 임시 비밀번호를 `<가림>`으로 바꿨습니다. 그래서 그 폴더의 `run.sh`는 그대로 다시 돌지 않습니다.
- Cloudflare Tunnel · LB · pool ID와 S3 버킷 이름은 그대로 둡니다. 비밀이 아닙니다(API 토큰 없이는 쓸 수 없음).
- 옮긴 뒤 `gitleaks dir docs/evidence/cloud-runs`: 유출 0.

## 옮기지 않은 것

| 원본(`deploy/pilot/runs/`) | 이유 |
|---|---|
| `cloud-state/` | 노트북 링크 관리자의 상태 파일(포트, PID, `kubectl port-forward` 로그)입니다. 실행 기록이 아니고, 지금 쓰지 않는 임시 링크(D-16 전)입니다 |
| `spike-rds-*`, `spike-cloudsql-*` | D-24 관리형 DB 시험입니다. `cloud-*` 기록이 아닙니다 |
| `smoke-*`, `contract-*` | 노트북 Compose 파일럿 기록입니다. 이미 [`../pilot-notebook/`](../pilot-notebook/README.md)에 있습니다 |
