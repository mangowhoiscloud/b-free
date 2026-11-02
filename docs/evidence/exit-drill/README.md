# Exit Drill (FR-15, T27)

스펙: 목적지 앱의 출발지 접근을 끊고, 재시작 · 새 연결까지 동작하는지 시험합니다. 복제 채널은 유지합니다. 출발지는 출발지 EKS(`bravo-source`), 목적지는 온프렘 RKE2(`bravo-onprem`)입니다.

실행: `deploy/cloud/exit_drill.sh`(2026-11-03 다시 씀). 예전 스크립트는 노트북 임시 링크(`link.sh`, pid 파일, `host.lima.internal:15434`)를 끊었는데, 지금 목적지의 출발지 경로는 D-16 링크 클라이언트 `chat/src-db-link`(→ `t1-src-db`)입니다. 그래서 지금 환경에서는 돌 수 없었습니다.

## 스크립트가 하는 일

| 단계 | 언제 | 내용 |
|---|---|---|
| 1. 사전 확인 | 항상(읽기만) | executor-0 `GET /v1/readiness`, 마지막 컷오버 실행, Argo 상태(`deploy/scripts/argo-state.sh`) |
| 2. 잔존 의존성 지도 | 항상(읽기만) | 클러스터에서 잰 값: PgBouncer 현재 대상과 재시작 때 기본 대상, 양쪽 DB의 구독과 연결 호스트(비밀번호는 읽지 않음), 목적지 링크 클라이언트, 앱 설정(DB · 첨부 · 비밀 · 텔레메트리), 이미지 레지스트리, 송신 NetworkPolicy, SecretStore, DB 백업 대상 |
| 3. 끊기 | executor-0가 `direction=reverse`이고 마지막 컷오버가 `DONE`일 때만 | `src-db-link`를 0으로 줄이고 앱 Pod에서 그 길이 막혔는지 확인. 출발지가 자기 `dst-db-link`로 목적지를 구독하는 역방향 복제는 그대로 |
| 4. 재시작 · 새 연결 | 3과 같음 | 목적지 앱 재시작. PgBouncer는 재시작 기본 대상이 목적지일 때만 재시작(아니면 FAIL과 이유. 재시작하면 끊긴 출발지를 가리켜 활성 환경이 멈추기 때문). `https://b-free-chat-dst.mangowhois.ai`로 쓰기 5 · 읽기 15 req/s 20초(`tools/loadgen` 앱 방식, 플랫폼 클러스터의 Job. 운영자 Mac에서 돌리지 않음), 목적지 DB에서 NFR-2, readiness가 여전히 reverse · streaming인지 |
| 5. 되돌리기 | 항상(끊었으면) | `src-db-link`를 원래 replica 수로. 스크립트가 중간에 멈춰도 EXIT trap이 되돌림 |

컷오버 전이면 1–2만 돌고 판정은 `NOT_RUN`(종료 코드 2)입니다.

## 실행 기록

| 폴더 | 시각(KST) | 판정 | 내용 |
|---|---|---|---|
| [20261103-030924/](20261103-030924/) | 11/3 03:09:24 | **NOT_RUN** | readiness `direction=forward`, 컷오버 실행 0건. 끊기는 S3 컷오버(사람 승인) 뒤에만 가능. 사전 확인, 의존성 지도, Argo 상태(`argo-state.txt`)만 기록 |

## 잔존 의존성 지도(03:09 측정, 컷오버 전)

| 의존성 | 측정값 | 어디를 가리킴 | 컷오버 뒤 |
|---|---|---|---|
| 목적지 PgBouncer 현재 대상 | `src-db-link.chat.svc.cluster.local:5432` | 출발지 DB | Executor가 `target-dst.ini`(`chat-db-rw`)로 바꿈 |
| 목적지 PgBouncer 재시작 기본 대상 | init 컨테이너가 `target-src.ini`를 복사 | 출발지 DB | **그대로 출발지.** 컷오버 뒤 PgBouncer Pod가 재시작되면 앱이 출발지(펜싱됨, 끊으면 연결 불가)를 다시 가리킴. Exit Drill의 PgBouncer 재시작 항목은 이것 때문에 FAIL이 예정됨 |
| 목적지 DB 구독 `bravo_fwd_sub` | enabled, 호스트 `src-db-link.chat.svc` | 출발지 DB | 컷오버가 멈춤 |
| 출발지 DB 구독 `bravo_rev_sub` | disabled, 호스트 `dst-db-link.chat.svc`(출발지 클러스터 안) | 목적지 DB | 컷오버가 켬. Exit Drill이 유지할 복제 채널 |
| 목적지 링크 클라이언트 | `src-db-link` → `t1-src-db.mangowhois.ai` | 출발지 DB | Exit Drill이 끊는 대상 |
| 앱 DB 주소 | `chat-pgbouncer.chat.svc.cluster.local` | 목적지 PgBouncer | 같음 |
| 앱 첨부 저장소 | `https://rook-ceph-rgw-bravo.rook-ceph.svc` | 목적지(RGW) | 같음 |
| 앱 DB 비밀번호 | `DB_PASSWORD_SOURCE=env-file` | 목적지 Secret | 같음(Secrets Manager 호출 없음) |
| 앱 텔레메트리 | `otel-agent.bravo-observability.svc:4317` | 목적지 에이전트 → 플랫폼 | 같음 |
| 이미지 | `bravo.local`(containerd에 가져옴), `ghcr.io`(CNPG), Docker Hub `cloudflare/cloudflared` | AWS 아님 | 같음 |
| 앱 송신 정책 `chat-bravo-demo` | PgBouncer, rook-ceph, bravo-observability, DNS만 | 인터넷 · AWS 직접 송신 없음 | 같음 |
| DB 백업 | `s3://bravo-chat-db-backup/` @ `rook-ceph-rgw-bravo` | 목적지(RGW) | 같음 |
| SecretStore | `ClusterSecretStore bravo-platform-openbao`(vault) | 플랫폼 OpenBao. 상태 `InvalidProviderConfig` | 같음 |

정리: 컷오버 뒤 목적지 앱이 출발지에 닿는 길은 `src-db-link` 하나입니다. 남는 문제는 PgBouncer 재시작 기본 대상이 출발지로 고정된 것입니다(`deploy/landing-zone`의 init 컨테이너). Executor는 ConfigMap 쓰기 권한이 없으므로(계약 9절), 기본 선택을 바꾸는 길을 따로 정해야 합니다.

## 남은 것

- 실제 끊기 · 재시작 · 새 연결은 사람 승인이 있는 S3 컷오버 뒤에 `deploy/cloud/exit_drill.sh`를 한 번 돌리면 됩니다. 판정과 근거는 `verdict.json`에 남습니다.
- PgBouncer 재시작 기본 대상(위 표)을 고치기 전에는 판정이 FAIL로 예상됩니다. 이유는 스크립트가 측정값으로 적습니다.
