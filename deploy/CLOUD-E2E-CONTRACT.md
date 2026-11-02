# 클라우드 E2E 공통 계약

여러 갈래가 동시에 클라우드 E2E(출발지 Amazon EKS → 온프렘 K3s)를 세울 때 서로 맞춰야 하는 이름, 경로, 규칙입니다. 류지환 지시(2026-11-02 20:10 KST): "클라우드까지 쭉 밀어. 공격적으로 병렬 작업하고 빠지는 파트 없도록. 스텁이나 가짜 정보는 막는다."

## 1. 금지

- 목, 스텁, 가짜 응답, 하드코딩한 결과 값, `sleep`으로 성공을 흉내 내는 코드.
- 실제로 연결되지 않은 것은 "연결 안 됨"과 이유를 그대로 보이고 기록합니다.
- 비밀 값을 파일, Terraform 상태, 로그, 채팅, 에이전트 프롬프트에 두는 것(D-26). 비밀은 플랫폼 OpenBao에서만 꺼냅니다.

## 2. 환경

| 환경 ID | 실체 | kubectl 컨텍스트 | OpenBao 경로 |
|---|---|---|---|
| `source-aws` | Amazon EKS `bravo-source`(ap-northeast-2, arm64) | `bravo-source` | `secret/envs/source-aws/*` |
| `target-onprem` | Lima VM 위 K3s | 온프렘 갈래가 정함(보고 필수) | `secret/envs/target-onprem/*` |
| 플랫폼 | 노트북 Compose `bravo-pilot` | - | `secret/platform/*` |

## 3. 클러스터 안 이름(두 환경 같음, D-23)

| 대상 | 이름 |
|---|---|
| 앱 네임스페이스 | `chat` |
| 데모 앱 | Helm 릴리스 `chat`(차트 `apps/demo/chart`), Service는 차트가 만드는 이름 그대로 보고 |
| DB | CNPG `Cluster` `chat-db`(ns `chat`), PostgreSQL 16, `wal_level=logical` |
| PgBouncer | `bravo-pgbouncer` 이미지, Deployment `chat-pgbouncer`(ns `chat`). Executor가 `pods/exec`로 대상 전환 |
| 진입 | Gateway API `Gateway` `bravo-gateway`(ns `bravo-edge`). Cilium 구현이면 Service는 `cilium-gateway-bravo-gateway`. 만들 수 없으면 터널이 앱 Service로 바로 가고, 그 사실을 보고 |
| Cloudflare 연결기 | ns `cf-edge`, Deployment `cloudflared`(이미지 digest 고정), Secret `cf-tunnel`의 키 `token` |

## 4. 비밀 전달

- 터널 토큰은 OpenBao `secret/envs/<env>/cf-tunnel`(필드 `token`)에 이미 있습니다.
- 전달은 플랫폼이 합니다. `deploy/pilot/scripts/keys.sh`의 값이 파이프로만 `kubectl create secret ... --from-file=token=/dev/stdin` 같은 명령에 들어갑니다. 파일이나 인자로 남기지 않습니다.
- ESO가 플랫폼 OpenBao를 당겨 오는 방식은 링크가 선 뒤에 바꿉니다. 지금은 OpenBao가 노트북의 127.0.0.1에만 열려 있습니다.

## 5. 이미지

- 출발지는 출발지 계정의 Amazon ECR(Terraform `infra/envs/source-aws`)입니다. 고객이 원래 쓰던 레지스트리라는 설정입니다.
- 목적지(온프렘)는 이미지 tar를 K3s containerd로 가져옵니다(폐쇄망 방식). AWS에 의존하지 않습니다.
- 두 쪽 모두 digest로 고정합니다.

## 6. 앞단

- `b-free.mangowhois.ai`가 대표 이름입니다. LB 구독 전에는 활성 환경의 터널을 가리키는 CNAME 하나입니다(`infra/envs/edge`).
- 전환은 Executor가 Cloudflare API로 합니다. 토큰은 OpenBao에서 그 실행 동안만 받습니다.
- `b-free-src`, `b-free-dst`는 환경별 직접 이름입니다.
- 터널 원격 설정의 `origin_service`는 3절의 진입 Service로 맞추고 `infra/envs/edge/terraform.tfvars`를 고칩니다.

## 7. 환경 사이 링크

- D-16 목표는 Cloudflare Tunnel + Access 서비스 토큰입니다. Zero Trust가 아직 꺼져 있어 지금은 쓸 수 없습니다.
- 그전까지 쓰는 임시 링크는 플랫폼(노트북)의 `kubectl port-forward`입니다. 목적지 K3s(Lima)는 `host.lima.internal`로 노트북 포트에 닿습니다.
- 임시 링크는 문서와 콘솔에 "임시 링크(D-16 전)"로 표시합니다.

## 8. 스펙 r2가 더한 계약(`docs/spec/sources/spec-v0.1.md` r2 절)

| 대상 | 계약 |
|---|---|
| 쓰기 차단 스위치(데모 앱) | 관리 포트 8081(앱 Service와 분리, ClusterIP Service `chat-admin`, 터널로 내보내지 않음). `GET /admin/writes` → `{"blocked": bool, "since": ts}`, `PUT /admin/writes {"blocked": true}` → 새 업무 쓰기를 503 + `Retry-After`로 거절, 읽기 유지. 인증은 Bearer, 값은 OpenBao `secret/envs/<env>/chat-admin`(필드 `token`)에서 플랫폼이 Secret `chat-admin`으로 전달 |
| 승인 2종 | `change`(S2: REHEARSE 통과 뒤, diff · 이미지 digest · 정책 diff 결속) → 목적지 반영 → 게이트 재대조. `cutover`(S3: 계획 · 게이트 결과 결속). `failback`은 그대로. 승인은 콘솔에서만, 요청자 ≠ 승인자 |
| 승인 반영 경로(T31) | 승인된 diff → 신뢰된 빌드(플랫폼 쪽, syft · trivy) → 서명(cosign, 키는 플랫폼 OpenBao transit) → 목적지 레지스트리 → digest가 승인 digest와 같을 때만 배포 → 번역된 정책 적용 → 게이트 재대조. GitHub Actions 경로는 저장소 push 뒤 같은 단계로 |
| 첨부 동기화(T8) | 컷오버 직전 1회 S3 → RGW, 데모 중 새 업로드 없음, 되돌리기 대상은 DB |
| 비밀 이관(T9) | Secrets Manager 값 → 플랫폼 OpenBao `secret/envs/target-onprem/chat/*` 1회. 값은 파이프로만 |

## 9. Executor가 두 클러스터에 기대는 것(데이터 · 컷오버 갈래, 2026-11-02 20:30)

Executor는 노트북에서 돕니다(플랫폼). 아래가 없으면 컷오버를 시작하지 않고 "연결 안 됨"과 이유를 기록합니다.

| 대상 | 계약 |
|---|---|
| 채팅 DB 스키마 | `apps/demo/schema/sql`의 `01_schema.sql` → `02_roles.sql` → `03_<side>.sql`을 그대로 적용(출발지 `src`, 목적지 `dst`). 역할 `app_writer`, `replicator`, `executor_ctl`, `executor_observe`. 목적지 `app_writer`는 `NOLOGIN`, 역방향 발행 `bravo_rev`와 슬롯 `bravo_rev_sub`는 목적지에. 구독은 Executor 쪽 `repl-setup`이 링크를 세운 뒤 만듦 |
| DB 역할 비밀번호 | OpenBao `secret/envs/<env>/chat-db`, 필드 `app_writer`, `replicator`, `executor_ctl`, `executor_observe`. 클러스터에는 플랫폼이 파이프로 Secret을 만듦. Executor는 실행 동안만 AppRole `executor`로 읽음 |
| CNPG `chat-db` | `wal_level=logical`, `max_slot_wal_keep_size` 설정, `max_replication_slots ≥ 10`. 서비스 `chat-db-rw`(5432) |
| PgBouncer `chat-pgbouncer` | 이미지 `bravo-pgbouncer`(`deploy/pilot/pgbouncer/Dockerfile`, `sh` · `cp` · `mv` 있음). replica 1. 대상 두 벌 `target-src.ini`, `target-dst.ini`(각각 `[databases]` 절만)을 ConfigMap 디렉터리 `/etc/pgbouncer/targets/`로 마운트(subPath 금지). 주 설정 `pgbouncer.ini` 끝에 `%include /run/pgbouncer/active.ini`. `/run/pgbouncer`는 emptyDir이고 init 컨테이너가 기본 선택을 복사(출발지 클러스터 `target-src.ini`, 목적지 클러스터도 처음은 `target-src.ini` = 링크 경유 출발지 DB). `admin_users = pgb_admin`, `stats_users = pgb_stats`, `pool_mode = transaction`, `query_wait_timeout = 8`. Service `chat-pgbouncer`(6432) |
| Executor의 K8s 권한 | ns `chat`에서 `pods/exec`(PgBouncer Pod), `pods/portforward`(`chat-db`, `chat-pgbouncer`, `chat-admin` Pod), `pods` get/list. ConfigMap 쓰기 없음 |
| 쓰기 차단 스위치 | 8절 그대로. Executor는 `chat-admin`(8081)에 port-forward로 닿고 토큰은 OpenBao `secret/envs/<env>/chat-admin` |
| 목적지 앱의 DB 경로(컷오버 전) | 목적지 PgBouncer `target-src.ini`의 host는 링크 끝 `host.lima.internal`, port는 플랫폼이 연 출발지 DB 포워드 포트(임시 링크(D-16 전)) |
