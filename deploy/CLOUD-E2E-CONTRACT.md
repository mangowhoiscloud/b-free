# 클라우드 E2E 공통 계약

여러 갈래가 동시에 클라우드 E2E(출발지 Amazon EKS에서 온프렘 RKE2로. 2026-11-02 21:10 전에는 K3s)를 세울 때 서로 맞춰야 하는 이름, 경로, 규칙입니다. 류지환 지시(2026-11-02 20:10 KST): "클라우드까지 쭉 밀어. 공격적으로 병렬 작업하고 빠지는 파트 없도록. 스텁이나 가짜 정보는 막는다."

## 1. 금지

- 목, 스텁, 가짜 응답, 하드코딩한 결과 값, `sleep`으로 성공을 흉내 내는 코드.
- 실제로 연결되지 않은 것은 "연결 안 됨"과 이유를 그대로 보이고 기록합니다.
- 비밀 값을 파일, Terraform 상태, 로그, 채팅, 에이전트 프롬프트에 두는 것(D-26). 비밀은 플랫폼 OpenBao에서만 꺼냅니다.

## 2. 환경

| 환경 ID | 실체 | kubectl 컨텍스트 | OpenBao 경로 |
|---|---|---|---|
| `source-aws` | Amazon EKS `bravo-source`(ap-northeast-2, arm64) | `bravo-source` | `secret/envs/source-aws/*` |
| `target-onprem` | Ubuntu VM 위 RKE2(CIS 프로필), 공통 cloud-init(T2). 2026-11-02 21:10 KST 결정(ADR 0010). 결정 전 Lima K3s(v1.36.5+k3s1)가 같은 컨텍스트로 아직 떠 있음 | `bravo-onprem`(RKE2로 바뀌어도 같은 이름) | `secret/envs/target-onprem/*` |
| `target-gcp` | GKE `bravo-target-gcp`(프로젝트 `b-free-target-20261102`, asia-northeast1-b) | 아직 보고 없음 | `secret/envs/target-gcp/*` |
| 플랫폼 | VPC ②의 EC2 1대 + RKE2 단일 노드(T7, 스펙 §7.2, ADR 0010) | RKE2 컨텍스트는 아직 없음. 생기면 이 칸에 적음. 결정 전 EKS `bravo-platform`(컨텍스트 `bravo-platform`)이 떠 있음 | `secret/platform/*` |

플랫폼 줄의 옛 값(노트북 Compose `bravo-pilot`)은 6.1절에 따라 내렸습니다.

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
- 목적지(온프렘)는 이미지 tar를 클러스터 containerd로 가져옵니다(폐쇄망 방식. K3s 때와 같고, RKE2도 containerd). AWS에 의존하지 않습니다.
- 두 쪽 모두 digest로 고정합니다.

## 6. 앞단(이름 바뀜, 류지환 지시 2026-11-02 20:4x)

| 이름 | 무엇 | 터널 | OpenBao 토큰 |
|---|---|---|---|
| `b-free.mangowhois.ai` | **우리 플랫폼**(콘솔 · API · MCP). 플랫폼 EKS의 Gateway `bravo-gateway`(ns `bravo-edge`)로 | `bravo-platform` | `secret/envs/platform/cf-tunnel` |
| `b-free-chat.mangowhois.ai` | 고객 데모 앱 대표 이름. LB 전에는 활성 환경 터널을 가리키는 CNAME, 전환은 Executor(`front.py`). **코드만, 미실행**: CNAME 전환과 LB 가중치 모두 실제 실행 기록이 없음(감사 B21, 2026-11-02 20:4x) | - | - |
| `b-free-chat-src.mangowhois.ai` | 출발지(EKS) 앱 | `bravo-b-free-chat-src` | `secret/envs/source-aws/cf-tunnel` |
| `b-free-chat-dst.mangowhois.ai` | 목적지(온프렘 RKE2, 결정 전에는 K3s) 앱 | `bravo-b-free-chat-dst` | `secret/envs/target-onprem/cf-tunnel` |

- 플랫폼 API는 Cloudflare Access JWT만 받습니다. 공개로 우회하는 길을 만들지 않습니다.
- 터널 원격 설정의 `origin_service`는 3절의 진입 Service로 맞추고 `infra/envs/edge/terraform.tfvars`를 고칩니다.

**LB(2026-11-02 20:4x 적용, `lb_enabled = true`).** LB `b-free-chat.mangowhois.ai`(random steering), pool `bravo-b-free-chat-src`(가중치 1, fallback) · `bravo-b-free-chat-dst`(가중치 0), 모니터 HTTPS `GET /healthz` 200, 60초. Host 헤더는 pool 끝점의 환경 이름이 모니터보다 우선합니다. 대표 CNAME은 비상 경로로 남아 있습니다. 요금제는 끝점 2개 포함이라 `lb_environments = ["src", "dst"]`로 gke pool은 만들지 않았습니다(gke를 넣으면 3개). 20:5x 상태: dst pool 정상(200), src pool 비정상(530, `bravo-b-free-chat-src` 터널에 커넥터 없음). 그래서 대표 이름은 530(1033)입니다. 가중치는 Executor가 바꿉니다.

**Access 설정 값(2026-11-02 20:5x 적용, `infra/envs/edge/access.tf`).** 비밀이 아닙니다. 플랫폼 API · `operator-mcp`가 `Cf-Access-Jwt-Assertion`을 검증할 때 씁니다.

| 설정(`platform/api` 환경 변수) | 값 |
|---|---|
| `BRAVO_JWT_ISSUER`(iss) | `https://falling-rain-1f1c.cloudflareaccess.com` |
| JWKS(`BRAVO_JWKS_PATH`가 가리킬 파일의 원본) | `https://falling-rain-1f1c.cloudflareaccess.com/cdn-cgi/access/certs`(RS256 키 2개, 교체됨) |
| `BRAVO_CONSOLE_AUD`(Access 앱 `bravo-platform`, 도메인 `b-free.mangowhois.ai`) | `bcaacf0741fb858e899a672ebd8f93e0fdf35c8ddf112b85bec7cbf6c2493443` |
| `BRAVO_MCP_AUD`(Access 앱 `bravo-platform-mcp`, 도메인 `b-free.mangowhois.ai/mcp`) | `59e040227a6e74ec26645f184927486ae144bf1244b77ee610a2b5bc8691b523` |
| `BRAVO_MCP_ALLOWED_HOSTS` | `b-free.mangowhois.ai` |

- 두 앱 모두 정책 `bravo platform operators`(Allow, 팀장 이메일 하나. 이메일은 gitignore된 `terraform.tfvars`에만)와 로그인 방법 One-time PIN 하나만 받습니다.
- MCP 앱은 Access Managed OAuth를 켰습니다. DCR 허용(localhost · loopback 리디렉트), 접근 토큰 15분, grant 세션 168시간. 토큰 없는 요청은 `401` + `WWW-Authenticate: Bearer … resource_metadata="https://b-free.mangowhois.ai/.well-known/cloudflare-access-protected-resource/mcp"`. 인가 서버 메타데이터는 `https://falling-rain-1f1c.cloudflareaccess.com/.well-known/oauth-authorization-server`.
- 콘솔과 MCP가 같은 호스트이고 경로로 나뉩니다(`08-operator-mcp.md`의 `mcp.<도메인>` 안과 다름). 그래서 API는 호스트가 아니라 경로로 허용 AUD를 고릅니다. `/mcp`는 MCP AUD만, 나머지는 콘솔 AUD만 받습니다.
- Managed OAuth로 들어온 요청의 실제 `aud`는 첫 실제 연결에서 확인합니다(`08` 12절).
- `bravo-platform` 터널에 아직 커넥터가 없어 `b-free`는 로그인 뒤 530입니다. 커넥터는 Access 앱이 있는 지금부터 띄워도 됩니다.

## 6.1 노트북 Compose 파일럿은 내림

류지환 지시(2026-11-02): "compose는 다 내려. 혼선을 주니까." 실제 대상은 출발지 EKS, 온프렘 RKE2, GKE, 플랫폼 RKE2뿐입니다(2026-11-02 21:10 결정 전에는 온프렘 K3s, 플랫폼 EKS). 예외는 플랫폼 키 보관소 `platform-bao` 하나입니다. 플랫폼 EKS로 옮기는 동안만 노트북에서 돌고, UI는 없습니다. 시험이 잠깐 띄우고 지우는 컨테이너는 괜찮습니다.

## 7. 환경 사이 링크

- D-16 목표는 Cloudflare Tunnel + Access 서비스 토큰입니다. Zero Trust Free가 2026-11-02 20:5x에 켜졌고, Cloudflare 쪽(Access 앱, Service Auth 정책, 관리 Tunnel, DNS)은 적용했습니다(`infra/envs/edge/links.tf`, `infra/modules/edge_link/cloudflare`).
- 클러스터 쪽도 올라가 있습니다(2026-11-02 23:3x 확인). 노출 환경의 관리 Tunnel 커넥터 `cf-edge/cloudflared-mgmt`(출발지 EKS, 온프렘 RKE2 각 2/2)와 플랫폼의 `bravo-link` 링크(`deploy/platform/link/d16`, Argo CD 앱 `link-d16`, 링크마다 2/2)입니다.
- 플랫폼에서 두 클러스터 API로 가는 길은 D-16 링크뿐입니다. 출발지 EKS API는 `src-k8s.bravo-link.svc:443`(`t1-src-k8s`)입니다. 수집기(`GET /v1/clusters`, 스냅숏)와 Executor가 씁니다. 인증은 바뀌지 않았습니다. EKS IAM 토큰(웹 아이덴티티 역할 + access entry `bravo-collector` · `bravo-executor`)입니다. TLS는 EKS CA로 확인합니다. 확인하는 이름은 수집기가 실제 EKS 엔드포인트 호스트(`eks:DescribeCluster`), Executor가 `kubernetes.default.svc`입니다. 둘 다 EKS API 인증서의 SAN에 있습니다. 온프렘 API는 `dst-k8s.bravo-link.svc:443`(`t1-dst-k8s`)이고 수집기, 리허설 서비스, Executor가 씁니다.
- 출발지 EKS 공개 엔드포인트 허용 목록에는 운영자 노트북 IP 하나만 있습니다. 플랫폼 NAT IP(`43.200.202.252/32`)는 2026-11-02 23:44 KST에 뺐습니다(`infra/envs/source-aws`, 증거 `docs/evidence/d16-source-k8s/`). 뺀 뒤에도 콘솔의 클러스터 화면 데이터(`GET /v1/clusters`)가 출발지를 읽습니다.
- 플랫폼 게이트는 2026-11-03부터 환경마다 자기 읽기 신원을 씁니다(수집기와 분리, `deploy/platform/link/gate-rbac/README.md`, `deploy/platform/scripts/gate-credentials.sh`). 출발지는 웹 아이덴티티 역할 `gate-ro` + access entry `bravo-gate`(`src-k8s`), 온프렘은 SA `kube-system/bravo-gate` 토큰(`dst-k8s`), GKE는 Google SA `bravo-gate-ro`(WIF, 링크 `gke-k8s`)입니다. DB는 세 환경 모두 역할 `gate_ro`로 DB 링크(`src-db`, `dst-db`, `gke-db`)를 씁니다. 첨부 저장소는 출발지가 canary 한 개만 읽는 게이트 역할, 온프렘이 링크 `dst-s3` · `dst-s3http`, GKE가 GCS XML API입니다. 측정 결과는 `docs/evidence/gate/`에 있습니다.
- 더한 링크(2026-11-03): `t1-dst-s3http`(RGW 평문 수신 80, 게이트 OBJ-06만), `t1-gke-db`(GKE 관리 Tunnel `t1-gke-mgmt`, GKE chat-db, 게이트 DB 질문만), `t1-gke-k8s`(GKE API, 수집기 · 게이트. 커넥터 출구는 제어면 사설 주소 `10.30.0.3:443`, TLS는 클러스터 CA와 `kubernetes.default.svc`). 토큰 `plat-dst-s3http`(`cd56a1d1-36bf-4091-8fcb-dc2a4ed2a142`), `plat-gke-db`(`9ce6fb45-3c30-4b8f-9ebc-a632678318f7`), `plat-gke-k8s`(`f564c8b8-0bf0-4a8a-b40d-5abfa3e016c6`), 만료 2026-11-09 16:0x–16:2x UTC.
- GKE 수집기: Google SA `bravo-cost-reader`(WIF)로 `gke-k8s`를 읽습니다(`deploy/platform/link/gke-rbac/collector.yaml`). 게이트는 `bravo-gate-ro`입니다.
- 아직 남은 임시 링크(노트북 `kubectl port-forward`, `deploy/cloud/link.sh`)는 문서와 콘솔에 "임시 링크(D-16 전)"로 표시합니다. 플랫폼에서 클러스터 API로 가는 길에는 없습니다.

**링크 이름과 토큰(D-16, 2026-11-02 20:5x).** 호스트는 `t1-<노출 환경>-<서비스>.mangowhois.ai`, 토큰은 `<소비>-<노출 환경>-<서비스>`(`02-network.md` 4.1)입니다. 토큰은 `keys.sh issue-service-token`이 만들어 OpenBao에만 씁니다. 필드 `client_id`, `client_secret`, `token_id`, `name`, `expires_at`. 만료 2026-11-09 11:50 UTC(168시간). Terraform에는 ID만 들어갑니다.

| 링크 호스트 | 원본(노출 클러스터 안) | 토큰(ID) | OpenBao |
|---|---|---|---|
| `t1-src-k8s` | `tcp://kubernetes.default.svc:443` | `plat-src-k8s`(`ab5f3dc8-5be9-4f52-a9ad-3eac65b5e5b0`) | `secret/envs/platform/cf-link-src-k8s` |
| `t1-src-db` | `tcp://chat-db-rw.chat.svc:5432` | `plat-src-db`(`cfb015e8-470a-42e5-8b4c-2fff897caf64`), `dst-src-db`(`0aeac4d3-0311-4171-8597-31dfcac6e0db`) | `secret/envs/platform/cf-link-src-db`, `secret/envs/target-onprem/cf-link-src-db` |
| `t1-src-pgb` | `tcp://chat-pgbouncer.chat.svc:6432` | `plat-src-pgb`(`360b174a-52c7-44ac-833d-91c1e58488c1`) | `secret/envs/platform/cf-link-src-pgb` |
| `t1-dst-k8s` | `tcp://kubernetes.default.svc:443` | `plat-dst-k8s`(`0cd17532-f7c5-4a3a-a447-9e6fb993d3e4`) | `secret/envs/platform/cf-link-dst-k8s` |
| `t1-dst-db` | `tcp://chat-db-rw.chat.svc:5432` | `plat-dst-db`(`8a6f2479-e17b-4c3b-9376-7723151252c2`), `src-dst-db`(`800eca3c-5f46-4406-be1f-7780e6cd5630`) | `secret/envs/platform/cf-link-dst-db`, `secret/envs/source-aws/cf-link-dst-db` |
| `t1-dst-pgb` | `tcp://chat-pgbouncer.chat.svc:6432` | `plat-dst-pgb`(`d4ea5882-3a6d-499b-99d7-ba2a7ba085cb`) | `secret/envs/platform/cf-link-dst-pgb` |

- 관리 Tunnel: `t1-src-mgmt`(`17ecaa76-0ed9-44a0-93a1-e84ffc50b714`), `t1-dst-mgmt`(`eac116b0-c06d-4e05-a25b-bbb162bc5e5f`). `config_src = local`이라 Cloudflare 쪽에서 라우트를 더할 수 없습니다. 커넥터 토큰 경로는 `secret/envs/<env>/cf-mgmt-tunnel`인데, `keys.sh issue-tunnel-token`은 지금 `cf-tunnel`(사용자 진입)에만 씁니다. 커넥터를 띄우는 갈래가 경로를 받게 고쳐야 합니다.
- 토큰 없이 링크 호스트에 오면 403입니다(2026-11-02 20:5x, 6개 모두 확인).
- `plat-src-access`(`12893ba3-800a-40c9-8da1-54f1c2cb61bb`, `secret/envs/source-aws/cf-access-service-token`)는 시험 중 만든 환경 단위 토큰입니다. 어느 정책에도 없어 아무 데도 들어갈 수 없습니다. 쓰지 않으면 폐기합니다.

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
| 목적지 앱의 DB 경로(컷오버 전) | 목적지 PgBouncer `target-src.ini` = `chat = host=host.lima.internal port=15434 dbname=chat`(플랫폼이 연 출발지 DB 포워드, 앱 경로 전용. 임시 링크(D-16 전)) |
| 링크 포트(노트북 127.0.0.1, `deploy/cloud/cloud.env`) | 출발지 DB: 15432 Executor 제어, 15433 정방향 복제(목적지 구독), 15434 목적지 앱 경로. 목적지 DB 25432. PgBouncer 16432(src) · 26432(dst). `chat-admin` 18081 · 28081. 역방향 relay 에이전트 17070. Exit Drill(T27)은 15434만 끊고 15433은 둠 |
| 출발지 PgBouncer `target-dst.ini` | `chat = host=dst-db-link.chat.svc port=5432 dbname=chat`(역방향 relay, `tools/link/k8s/reverse-relay.yaml`) |
| 역방향 relay 이미지 | python3가 든 이미지(데모 앱 이미지면 됨)가 출발지 ECR에 있어야 함. `tools/link/k8s` kustomize에 digest를 넣음 |
| 객체 저장소(T8) | OpenBao `secret/envs/<env>/object-store`, 필드 `bucket`, `endpoint_url`(비우면 AWS), `region`, `access_key_id`, `secret_access_key`(비우면 SDK 기본 자격) |
| 실행 도구(데이터 · 컷오버, `deploy/cloud/`) | `bootstrap.sh check`(전제 조건, 안 되면 "연결 안 됨"과 이유) · `db_roles.py`(빈 DB에만 01–03 적용, 역할 비밀번호는 SCRAM 검증자로만 전달, Executor 역할 값은 OpenBao에 생성) · `link.sh`(정방향 포워드, 역방향 relay, OpenBao 포워드) · `repl_setup.sh` · `executor.sh` · `run_e2e.sh`(S3 · S4) · `contract.sh`(E1 · E4 · E5 · E6) · `t25.sh` · `exit_drill.sh`. 실행 기록은 `deploy/pilot/runs/cloud-*` |
| 승인(감사 A2) | Executor는 플랫폼 DB `approvals`에서 승인을 확인한 뒤에만 실행. 필요: platform/api가 `bravo_executor`에 `approvals` SELECT와 RLS 정책을 주고, `stage`에 `failback`을 더함. 승인 없는 클라우드 실행은 없음 |
