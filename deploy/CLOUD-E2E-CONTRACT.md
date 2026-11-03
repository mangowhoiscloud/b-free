# 클라우드 E2E 공통 계약

여러 갈래가 같은 클라우드 환경에서 일할 때 맞춰야 하는 이름, 경로, 규칙입니다. 류지환 지시(2026-11-02 20:10 KST): "클라우드까지 쭉 밀어. 공격적으로 병렬 작업하고 빠지는 파트 없도록. 스텁이나 가짜 정보는 막는다."

**기준 시각: 2026-11-03 02:40 KST.** 0절이 지금 실행 환경을 적는 유일한 표입니다. 명령과 주소는 0절과 1–9절에서만 가져갑니다. 10절 "역사"는 지난 관측이며 실행 지침이 아닙니다. 0절의 관측 원본은 `docs/evidence/cloud-runs/platform-snapshot-20261103T0232KST.txt`입니다.

## 0. 지금 실행 환경

### 0.1 환경

| 환경 ID | 실체 | kubectl 컨텍스트 | 앱 공개 이름(`/api/config`의 `env.id`, 02:32) | OpenBao 경로 | 플랫폼에서 가는 길 |
|---|---|---|---|---|---|
| `source-aws` | Amazon EKS `bravo-source`(ap-northeast-2), 노드 1(arm64), `v1.36.5-eks` | `bravo-source`(AWS 프로필은 저장소 스크립트가 쓰는 것) | `b-free-chat-src`(`aws`) | `secret/envs/source-aws/*` | D-16 링크 `src-k8s` · `src-db` · `src-pgb` · `src-admin` |
| `target-onprem` | 같은 Mac의 Lima VM 2대(`bravo-onprem-server`, `bravo-onprem-agent`) 위 RKE2 `v1.36.5+rke2r1` CIS 프로필. 블록 저장은 Rook-Ceph RBD(rbd-nbd), 첨부는 Rook-Ceph RGW(SSE-KMS, 키는 플랫폼 OpenBao transit), DB는 CNPG PG16 | `bravo-onprem`(API `https://127.0.0.1:16443`) | `b-free-chat-dst`(`onprem`) | `secret/envs/target-onprem/*` | `dst-k8s` · `dst-db` · `dst-pgb` · `dst-admin` · `dst-s3` · `dst-s3http` |
| `target-gcp` | GKE Standard `bravo-target-gcp`, 프로젝트 `b-free-target-20261102`, asia-northeast1-b, Arm 노드 | `bravo-target-gcp`. 기본 kubeconfig에 없음: `KUBECONFIG=infra/envs/target-gcp/.terraform/bravo/kubeconfig`, `gke-gcloud-auth-plugin` 필요 | `b-free-chat-gke`(`gcp`) | `secret/envs/target-gcp/*` | `gke-k8s` · `gke-db` |
| 플랫폼 | VPC ②의 EC2 1대 + RKE2 단일 노드 `bravo-platform`(`v1.36.5+rke2r1`, arm64), 공통 cloud-init(`infra/modules/cluster/onprem/cloud-init`). 스펙 §7.2, ADR 0010 | `bravo-platform-rke2`(SSM 포트 포워드 `https://127.0.0.1:26443`) | `b-free`(Access 로그인 302) | `secret/platform/*`, `secret/envs/platform/*` | - |

### 0.2 플랫폼 서비스

이미지는 `deploy/platform/control/kustomization.yaml`의 digest로만 바꿉니다. Argo CD가 main을 자동 동기화합니다(selfHeal). `kubectl set image`와 Deployment 손 수정은 하지 않습니다. 02:32에 모든 서비스의 실행 digest가 고정 값과 같았습니다.

| 이름(ns `bravo-control`) | 포트 | 하는 일 | 실행 digest(02:32) |
|---|---|---|---|
| `api` | 8080 | 플랫폼 API, 상태 머신, 승인 | `api@sha256:59f6ba2b…` |
| `operator-mcp` | 8082 | 운영자 MCP(`/mcp`, 같은 api 이미지) | `api@sha256:59f6ba2b…` |
| `console` | 8080 | 콘솔 | `console@sha256:91678860…` |
| `gate` | 8084 | 통제 게이트 | `gate@sha256:ec47f7c3…` |
| `collector` | 8083 | 읽기 전용 수집기 | `collector@sha256:af3c66c1…` |
| `rehearsal` | 8090 | 리허설 서비스(Harbor Trial) | `rehearsal@sha256:1e1337fa…` |
| `executor`(StatefulSet `executor-0`) | 8081 | Executor(컷오버, 되돌리기, 앞단 전환, 첨부 복사) | `executor@sha256:f5ec7dfd…` |
| `platform-db-rw` | 5432 | CNPG `platform-db`, DB `bravo`, 마이그레이션 0001–0008 | - |

다른 네임스페이스: `openbao`(`openbao-0`, 유일한 키 보관소, D-26), `bravo-observe`(관측), `bravo-edge`(Gateway), `bravo-link`(D-16 링크 클라이언트), `argocd`.

### 0.3 공개 이름

| 이름 | 무엇 | 경로 | 02:32 관측 |
|---|---|---|---|
| `b-free.mangowhois.ai` | **우리 플랫폼**(콘솔 · API, `/mcp`는 MCP). 고객 앱이 아님 | 플랫폼 Tunnel `bravo-platform` → Gateway `bravo-gateway`(ns `bravo-edge`) | 302, Access 로그인 |
| `b-free-chat.mangowhois.ai` | 고객 데모 앱 대표 이름. Cloudflare LB(아래) | pool `bravo-b-free-chat-src` · `bravo-b-free-chat-dst` | 200, `aws` |
| `b-free-chat-src.mangowhois.ai` | 출발지 EKS 앱 | Tunnel `bravo-b-free-chat-src` | 200, `aws` |
| `b-free-chat-dst.mangowhois.ai` | 온프렘 RKE2 앱 | Tunnel `bravo-b-free-chat-dst` | 200, `onprem` |
| `b-free-chat-gke.mangowhois.ai` | GKE 앱. **LB pool에 없음** | GKE Tunnel | 200, `gcp` |

**LB.** `b-free-chat.mangowhois.ai`, random steering, pool은 src · dst 둘(`infra/envs/edge` `lb_environments = ["src", "dst"]`). 요금제 포함량이 끝점 2개라 gke pool은 만들지 않았습니다. 모니터는 HTTPS `GET` 헬스 경로 200, 60초 간격. 가중치는 executor-0의 `POST /v1/front/shift`만 바꿉니다. `front.py`는 `b-free`(플랫폼) 전환을 거부합니다. 실제 실행 기록은 `docs/evidence/cloud-runs/`(10/50/100 이동 4회, 자동 0% 복귀 2회)입니다. 콘솔이나 상태 머신에서 시작한 적은 없습니다.

**대표 CNAME은 비상 경로일 뿐입니다.** LB를 끄면 같은 이름의 CNAME이 응답합니다(스펙 §8.7 "비상 경로는 DNS 전환"). CNAME은 0%와 100%만 바꿉니다. **10/50/100 단계가 없으므로 FR-8 · T17을 만족하지 않습니다**(`docs/spec/DEVIATIONS.md` D-10의 B23 메모). 데모의 S3 전환은 LB로만 합니다.

### 0.4 Access 신원(직무 분리)

| 신원 | 역할 | 정하는 곳 | 02:32 상태 |
|---|---|---|---|
| naver 메일 | `operator`, `viewer`(요청자: 계획 · 실행 시작) | OpenBao `secret/envs/platform/access-members` → Kubernetes Secret | **첫 로그인 전.** 역할은 첫 로그인 때만 생기므로 아직 없음 |
| gmail 메일 | `approver`, `viewer`(승인자) | 같음 | 로그인함. 플랫폼 DB `principals` 1행, `memberships` approver · viewer |

- 2026-11-03 팀 공유: 위 운영자·승인자 두 신원은 유지하고, 팀원 네 명의 Slack 이메일에 `viewer` 첫 로그인 역할을 추가했습니다. 실제 목록은 OpenBao에서 Secret으로 전달하며 Git의 `members.json`은 배포하지 않는 예시입니다. 역할 반영은 각자의 첫 로그인 때 일어납니다. [접근 절차](platform/access/README.md)
- 두 Access 앱(`bravo-platform`, `bravo-platform-mcp`)의 정책 `bravo platform operators`는 위 두 메일과 팀 리뷰 메일 네 개를 허용합니다(`infra/envs/edge/access.tf`의 `platform_access_emails`, 값은 gitignore된 `terraform.tfvars`에만). 로그인 방법은 One-time PIN 하나, 세션 24시간입니다. MCP Managed OAuth grant 세션은 168시간입니다.
- 목록에 없는 이메일은 403 `unknown_principal`입니다. 운영자와 승인자는 같은 신원일 수 없습니다(D-18).
- 두 메일 모두 팀장 한 사람의 것입니다. 분리는 신원 단위이고 사람 단위가 아닙니다. 무대에서 다른 팀원이 승인할지는 팀 결정입니다(리뷰 P8).

### 0.5 환경 사이 링크(D-16)

모두 Cloudflare Tunnel + Access 서비스 토큰입니다. 토큰은 `keys.sh issue-service-token`이 OpenBao에만 씁니다. 자세한 표는 `deploy/platform/link/d16/README.md`입니다.

| 쓰는 쪽 | 링크(로컬 Service) | 원본 | 쓰는 곳 |
|---|---|---|---|
| 플랫폼 | `src-k8s`, `dst-k8s`, `gke-k8s`(ns `bravo-link`, 443) | 각 클러스터 API | 수집기, 게이트, 리허설 서비스, Executor |
| 플랫폼 | `src-db`, `dst-db`, `gke-db`(5432) · `src-pgb`, `dst-pgb`(6432) | 각 `chat-db-rw`, `chat-pgbouncer` | Executor(verify-full), 게이트(`gate_ro`) |
| 플랫폼 | `src-admin`, `dst-admin`(8081) | 앱 관리 포트(쓰기 차단 스위치) | Executor |
| 플랫폼 | `dst-s3`, `dst-s3http` | 온프렘 RGW(TLS, 평문 80) | Executor 첨부 복사, 게이트 OBJ-* |
| 온프렘 | `chat/src-db-link` | 출발지 `chat-db-rw` | 컷오버 전 온프렘 앱 경로(`target-src.ini`), 정방향 구독 |
| 출발지 | `chat/dst-db-link` | 온프렘 `chat-db-rw` | 출발지 PgBouncer `target-dst.ini`, 역방향 구독 |
| GKE · 온프렘 RGW | `t1-plat-bao` | 플랫폼 OpenBao | GKE ESO, 온프렘 RGW SSE-KMS(transit 키 `rgw-target-onprem`) |
| 세 고객 클러스터 | `t1-plat-otel` | 플랫폼 OTel Collector | 관측 |

토큰 만료(발급 기록 기준): 첫 묶음 2026-11-09 11:50 UTC(11/9 20:50 KST), 01시대 추가분 2026-11-09 16:0x–16:2x UTC. 회전 연습은 아직 안 했습니다(리뷰 B16).

### 0.6 Executor가 보는 길

`executor-0`은 `deploy/cloud/in-cluster/topology.json`(ConfigMap `executor-topology`)을 읽습니다. 02:08 KST에 새 topology로 다시 떴습니다.

| 대상 | 지금 값 |
|---|---|
| DB | `src-db.bravo-link.svc` · `dst-db.bravo-link.svc`:5432, `sslmode=verify-full`, 환경별 CNPG CA |
| PgBouncer | `src-pgb` · `dst-pgb.bravo-link.svc`:6432(verify-full). 대상 전환은 `pods/exec`(`*-k8s` 링크) |
| 쓰기 차단 스위치 | `http://src-admin` · `dst-admin.bravo-link.svc:8081`, 토큰 OpenBao `secret/envs/<env>/chat-admin` |
| 복제 | 정방향 `src-db-link.chat.svc`(온프렘 쪽), 역방향 `dst-db-link.chat.svc`(출발지 쪽), 둘 다 verify-full |
| 첨부 저장소 | OpenBao `secret/envs/<env>/object-store` |
| PgBouncer 대상 파일(02:3x 관측) | 온프렘 `target-src.ini` = `src-db-link.chat.svc.cluster.local`, `target-dst.ini` = `chat-db-rw`. 출발지 `target-src.ini` = `chat-db-rw`, `target-dst.ini` = `dst-db-link.chat.svc.cluster.local` |

## 1. 금지

- 목, 스텁, 가짜 응답, 하드코딩한 결과 값, `sleep`으로 성공을 흉내 내는 코드.
- 실제로 연결되지 않은 것은 "연결 안 됨"과 이유를 그대로 보이고 기록합니다.
- 비밀 값을 파일, Terraform 상태, 로그, 채팅, 에이전트 프롬프트에 두는 것(D-26). 비밀은 플랫폼 OpenBao에서만 꺼냅니다.
- 승인 행을 스크립트로 만들거나 되돌리기를 스크립트로 시작하는 것. 승인은 콘솔에서 사람이, 요청자와 다른 신원이 합니다.

## 2. 환경 ID와 OpenBao 경로

0.1절 표가 정본입니다. 환경 ID(`source-aws`, `target-onprem`, `target-gcp`)는 OpenBao 경로, 게이트 레지스트리(`deploy/platform/control/config/environments.yaml`), 콘솔 표시에 같은 문자열로 씁니다.

## 3. 클러스터 안 이름(세 고객 클러스터 같음, D-23)

| 대상 | 이름 |
|---|---|
| 앱 네임스페이스 | `chat` |
| 데모 앱 | Deployment `chat-bravo-demo`(차트 `apps/demo/chart`) |
| DB | CNPG `Cluster` `chat-db`(ns `chat`), PostgreSQL 16, `wal_level=logical`, TLS만 받음(`hostnossl … reject`) |
| PgBouncer | `bravo-pgbouncer` 이미지, Deployment `chat-pgbouncer`(ns `chat`). Executor가 `pods/exec`로 대상 전환 |
| 진입 | Gateway API `Gateway` `bravo-gateway`(ns `bravo-edge`), Cilium 구현 |
| 사용자 진입 Tunnel 커넥터 | ns `cf-edge`, Deployment `cloudflared`(digest 고정), Secret `cf-tunnel`의 키 `token` |
| 관리 Tunnel 커넥터(D-16) | ns `cf-edge`, Deployment `cloudflared-mgmt`, Secret `cf-mgmt-tunnel` |

## 4. 비밀 전달

- 키와 비밀은 플랫폼 OpenBao에만 있습니다(D-26, ADR 0007).
- 출발지 · 온프렘: `deploy/pilot/scripts/keys.sh`가 값을 파이프로만 넘겨 Secret을 만듭니다(`kubectl apply --server-side`, 3b4a910). 파일이나 인자로 남기지 않습니다. 온프렘의 ClusterSecretStore `bravo-platform-openbao`는 주소가 `openbao.bravo-link.invalid`(링크 대기)라 Ready가 아닙니다(02:3x 관측). 출발지에는 ClusterSecretStore가 없습니다.
- GKE: ESO가 링크 `t1-plat-bao`로 플랫폼 OpenBao를 읽습니다(AppRole `eso-target-gcp`, `deploy/landing-zone/overlays/gke/README.md`). 이번에는 GKE를 직접 읽지 못해 상태를 다시 확인하지 않았습니다.

## 5. 이미지

스펙의 "Actions → GHCR → 양쪽이 같은 digest pull"과 다릅니다. 정식 편차는 `docs/spec/DEVIATIONS.md` D-28입니다.

| 대상 | 지금 경로 | 02:3x 관측 |
|---|---|---|
| 플랫폼 서비스 | 운영자가 buildx(linux/arm64)로 빌드 → ECR `721622471953.dkr.ecr.ap-northeast-2.amazonaws.com/bravo-platform/<name>` → `kustomization.yaml` digest → Argo CD | 0.2절과 같음. GitHub `platform-images`(GHCR → ECR 복사) 워크플로는 11/3 01:56–02:07 KST 실행이 모두 실패(`digests` 아티팩트 없음) |
| 데모 앱 · PgBouncer(출발지) | ECR `bravo-source/<name>` | `bravo-demo@sha256:b5c0e764…`, `bravo-pgbouncer@sha256:68c4a15e…` |
| 같은 이미지(온프렘) | OCI tar → 노드 containerd import(`deploy/landing-zone/install.sh`, 폐쇄망 방식). 이름 `bravo.local/<name>` | 출발지와 **같은 digest**(`b5c0e764…`, `68c4a15e…`) |
| 같은 이미지(GKE) | Artifact Registry `asia-northeast1-docker.pkg.dev/b-free-target-20261102/bravo/<name>` | 01:56 게이트 VAP 기록의 앱 digest는 `df08e969…`. 출발지 · 온프렘과 **다름** |
| S2 변경 반영(T31) | `platform/apply`: 플랫폼 쪽 빌드 → syft · trivy → cosign 서명(키 OpenBao transit `bravo-supply-chain`) → 승인 digest 대조 → 목적지 import · 배포 | 실행은 11/2 21:04 CLI 1회뿐(`docs/evidence/t31/`) |

모든 배포는 digest로 고정합니다. 태그로 배포하지 않습니다.

## 6. Access 앱 설정 값

비밀이 아닙니다. 플랫폼 API · `operator-mcp`가 `Cf-Access-Jwt-Assertion`을 검증할 때 씁니다(`infra/envs/edge/access.tf`).

| 설정(`platform/api` 환경 변수) | 값 |
|---|---|
| `BRAVO_JWT_ISSUER`(iss) | `https://falling-rain-1f1c.cloudflareaccess.com` |
| JWKS(`BRAVO_JWKS_PATH`가 가리킬 파일의 원본) | `https://falling-rain-1f1c.cloudflareaccess.com/cdn-cgi/access/certs` |
| `BRAVO_CONSOLE_AUD`(Access 앱 `bravo-platform`, 도메인 `b-free.mangowhois.ai`) | `bcaacf0741fb858e899a672ebd8f93e0fdf35c8ddf112b85bec7cbf6c2493443` |
| `BRAVO_MCP_AUD`(Access 앱 `bravo-platform-mcp`, 도메인 `b-free.mangowhois.ai/mcp`) | `59e040227a6e74ec26645f184927486ae144bf1244b77ee610a2b5bc8691b523` |
| `BRAVO_MCP_ALLOWED_HOSTS` | `b-free.mangowhois.ai` |

- 정책과 신원은 0.4절입니다.
- MCP 앱은 Access Managed OAuth를 켰습니다. DCR 허용(localhost · loopback 리디렉트), 접근 토큰 15분, grant 세션 168시간. 토큰 없는 요청은 `401` + `WWW-Authenticate: Bearer … resource_metadata="https://b-free.mangowhois.ai/.well-known/cloudflare-access-protected-resource/mcp"`.
- 콘솔과 MCP가 같은 호스트이고 경로로 나뉩니다. API는 경로로 허용 AUD를 고릅니다. `/mcp`는 MCP AUD만, 나머지는 콘솔 AUD만 받습니다.
- Managed OAuth로 들어온 요청의 실제 `aud`는 첫 실제 연결에서 확인합니다(`docs/architecture/08-operator-mcp.md` 12절).

## 7. 링크 이름과 토큰

호스트는 `t1-<노출 환경>-<서비스>.mangowhois.ai`, 토큰은 `<소비>-<노출 환경>-<서비스>`입니다(`docs/architecture/02-network.md` 4.1). 토큰 필드는 `client_id`, `client_secret`, `token_id`, `name`, `expires_at`입니다. Terraform에는 ID만 들어갑니다.

| 링크 호스트 | 원본(노출 클러스터 안) | 토큰(ID) | OpenBao |
|---|---|---|---|
| `t1-src-k8s` | `tcp://kubernetes.default.svc:443` | `plat-src-k8s`(`ab5f3dc8-5be9-4f52-a9ad-3eac65b5e5b0`) | `secret/envs/platform/cf-link-src-k8s` |
| `t1-src-db` | `tcp://chat-db-rw.chat.svc:5432` | `plat-src-db`(`cfb015e8-470a-42e5-8b4c-2fff897caf64`), `dst-src-db`(`0aeac4d3-0311-4171-8597-31dfcac6e0db`) | `secret/envs/platform/cf-link-src-db`, `secret/envs/target-onprem/cf-link-src-db` |
| `t1-src-pgb` | `tcp://chat-pgbouncer.chat.svc:6432` | `plat-src-pgb`(`360b174a-52c7-44ac-833d-91c1e58488c1`) | `secret/envs/platform/cf-link-src-pgb` |
| `t1-dst-k8s` | `tcp://kubernetes.default.svc:443` | `plat-dst-k8s`(`0cd17532-f7c5-4a3a-a447-9e6fb993d3e4`) | `secret/envs/platform/cf-link-dst-k8s` |
| `t1-dst-db` | `tcp://chat-db-rw.chat.svc:5432` | `plat-dst-db`(`8a6f2479-e17b-4c3b-9376-7723151252c2`), `src-dst-db`(`800eca3c-5f46-4406-be1f-7780e6cd5630`) | `secret/envs/platform/cf-link-dst-db`, `secret/envs/source-aws/cf-link-dst-db` |
| `t1-dst-pgb` | `tcp://chat-pgbouncer.chat.svc:6432` | `plat-dst-pgb`(`d4ea5882-3a6d-499b-99d7-ba2a7ba085cb`) | `secret/envs/platform/cf-link-dst-pgb` |
| `t1-dst-s3http` | RGW 평문 수신 80 | `plat-dst-s3http`(`cd56a1d1-36bf-4091-8fcb-dc2a4ed2a142`) | `secret/envs/platform/cf-link-dst-s3http` |
| `t1-gke-db` | GKE `chat-db-rw`:5432(관리 Tunnel `t1-gke-mgmt`) | `plat-gke-db`(`9ce6fb45-3c30-4b8f-9ebc-a632678318f7`) | `secret/envs/platform/cf-link-gke-db` |
| `t1-gke-k8s` | GKE 제어면 사설 주소 `10.30.0.3:443` | `plat-gke-k8s`(`f564c8b8-0bf0-4a8a-b40d-5abfa3e016c6`) | `secret/envs/platform/cf-link-gke-k8s` |

- `src-admin` · `dst-admin` · `dst-s3`의 호스트와 토큰은 `deploy/platform/link/d16/links.yaml`에 있습니다.
- 관리 Tunnel: `t1-src-mgmt`(`17ecaa76-0ed9-44a0-93a1-e84ffc50b714`), `t1-dst-mgmt`(`eac116b0-c06d-4e05-a25b-bbb162bc5e5f`). `config_src = local`이라 라우트는 `deploy/platform/link/customer-mgmt-tunnel`의 설정 파일로 더합니다.
- 토큰 없이 링크 호스트에 오면 403입니다(2026-11-02 20:5x, 첫 6개 확인).
- 출발지 EKS 공개 엔드포인트 허용 목록에는 운영자 노트북 IP 하나만 있습니다(비상 경로). 플랫폼 NAT IP는 2026-11-02 23:44 KST에 뺐습니다(`docs/evidence/d16-source-k8s/`).
- 게이트는 환경마다 자기 읽기 신원을 씁니다(`deploy/platform/link/gate-rbac/README.md`, `deploy/platform/scripts/gate-credentials.sh`).
- `plat-src-access`(`12893ba3-800a-40c9-8da1-54f1c2cb61bb`)는 시험 중 만든 토큰이고 어느 정책에도 없습니다. 쓰지 않으면 폐기합니다.

## 8. 스펙 r2가 더한 계약(`docs/spec/sources/spec-v0.1.md` r2 절)

| 대상 | 계약 |
|---|---|
| 쓰기 차단 스위치(데모 앱) | 관리 포트 8081(앱 Service와 분리, ClusterIP Service `chat-admin`, 사용자 진입 Tunnel로 내보내지 않음). `GET /admin/writes` → `{"blocked": bool, "since": ts}`, `PUT /admin/writes {"blocked": true}` → 새 업무 쓰기를 503 + `Retry-After`로 거절, 읽기 유지. 인증은 Bearer, 값은 OpenBao `secret/envs/<env>/chat-admin`(필드 `token`) |
| 승인 2종 | `change`(S2: REHEARSE 통과 뒤, diff · 이미지 digest · 정책 diff 결속) → 목적지 반영 → 게이트 재대조. `cutover`(S3: 계획 · 게이트 결과 결속). `failback`은 그대로. 승인은 콘솔에서만, 요청자 ≠ 승인자 |
| 승인 반영 경로(T31) | 승인된 diff → 신뢰된 빌드(플랫폼 쪽, syft · trivy) → 서명(cosign, 키는 플랫폼 OpenBao transit) → 목적지 레지스트리 → digest가 승인 digest와 같을 때만 배포 → 번역된 정책 적용 → 게이트 재대조. 스펙의 Actions → GHCR 경로와 다름(D-28) |
| 첨부 동기화(T8) | 컷오버 직전 1회 S3 → RGW, 데모 중 새 업로드 없음, 되돌리기 대상은 DB |
| 비밀 이관(T9) | Secrets Manager 값 → 플랫폼 OpenBao `secret/envs/target-onprem/chat/*` 1회. 값은 파이프로만 |

## 9. Executor가 두 클러스터에 기대는 것

Executor는 플랫폼의 `executor-0`입니다(0.6절). 아래가 없으면 컷오버를 시작하지 않고 "연결 안 됨"과 이유를 기록합니다.

| 대상 | 계약 |
|---|---|
| 채팅 DB 스키마 | `apps/demo/schema/sql`의 `01_schema.sql` → `02_roles.sql` → `03_<side>.sql`(출발지 `src`, 목적지 `dst`). 역할 `app_writer`, `replicator`, `executor_ctl`, `executor_observe`. 목적지 `app_writer`는 `NOLOGIN`. 역방향 발행 `bravo_rev`와 슬롯 `bravo_rev_sub`는 목적지에. 구독은 `deploy/cloud/repl_setup.sh`가 링크를 세운 뒤 만듦 |
| DB 역할 비밀번호 | OpenBao `secret/envs/<env>/chat-db`, 필드 `app_writer`, `replicator`, `executor_ctl`, `executor_observe`. DB에는 SCRAM 검증값만(`deploy/cloud/db_roles.py`). Executor는 실행 동안만 AppRole `executor`로 읽음 |
| CNPG `chat-db` | `wal_level=logical`, `max_slot_wal_keep_size` 설정, `max_replication_slots ≥ 10`, `wal_sender_timeout` · `wal_receiver_timeout` 60초(`docs/evidence/repl-timeouts/`). 서비스 `chat-db-rw`(5432) |
| PgBouncer `chat-pgbouncer` | 대상 두 벌 `target-src.ini`, `target-dst.ini`(각각 `[databases]` 절만)를 ConfigMap 디렉터리 `/etc/pgbouncer/targets/`로 마운트(subPath 금지). 주 설정 끝에 `%include /run/pgbouncer/active.ini`. `/run/pgbouncer`는 emptyDir, init 컨테이너가 처음에 `target-src.ini`를 복사(목적지 클러스터도 처음은 링크 경유 출발지 DB). `admin_users = pgb_admin`, `stats_users = pgb_stats`, `pool_mode = transaction`, `query_wait_timeout = 8`. 서버 쪽 `server_tls_sslmode = verify-full` |
| Executor의 K8s 권한 | ns `chat`에서 `pods/exec`(PgBouncer 대상 전환), `pods/portforward`, `pods` get/list. ConfigMap · Secret 권한 없음(`deploy/landing-zone/base/chat-pgbouncer.yaml`, `deploy/platform/link/onprem-rbac/executor.yaml`). 온프렘 토큰은 OpenBao `secret/envs/target-onprem/k8s-executor`에만 |
| 승인(감사 A2) | Executor는 플랫폼 DB `approvals`에서 승인을 확인한 뒤에만 컷오버 · 되돌리기를 실행(`approvals.py`). 승인이 없으면 거부. 02:32 `approvals` 0행 |
| 객체 저장소(T8) | OpenBao `secret/envs/<env>/object-store`, 필드 `bucket`, `endpoint_url`(비우면 AWS), `region`, `access_key_id`, `secret_access_key` |
| 실행 도구(`deploy/cloud/`) | `bootstrap.sh check` · `db_roles.py` · `repl_setup.sh` · `front_shift.sh` · `run_e2e.sh`(S3 · S4, 컷오버 · 되돌리기는 콘솔 시작을 기다림) · `contract.sh`(E1 · E4 · E5 · E6) · `fault.sh` · `t25.sh` · `exit_drill.sh`. 실행 기록은 `deploy/pilot/runs/cloud-*`(Git 제외)에 생기므로 `docs/evidence/cloud-runs/`로 옮김 |

## 10. 역사(실행 지침 아님)

아래는 지난 관측과 내린 구성입니다. 지금 명령이나 주소로 쓰지 않습니다. 지난 실행 기록은 `docs/evidence/cloud-runs/history-k3s-20261102T2049-2101/`에 있습니다.

### 10.1 2026-11-02 21:10 KST 전: 온프렘 K3s, 플랫폼 EKS

- 온프렘은 Lima VM 2대 위 K3s v1.36.5+k3s1이었습니다. 21:10에 팀장이 RKE2를 스펙 r2대로 되돌렸습니다(ADR 0010, D-21 부분 철회). 컨텍스트 이름 `bravo-onprem`은 그대로 썼습니다.
- 플랫폼은 Amazon EKS `bravo-platform`(컨텍스트 `bravo-platform`, 노드 그룹 core 3 · agents 1)이었습니다. 지금은 EC2 + RKE2(`bravo-platform-rke2`)이고 EKS 컨텍스트는 노트북 kubeconfig에 없습니다.

### 10.2 노트북 Compose 파일럿(내림)

류지환 지시(2026-11-02): "compose는 다 내려. 혼선을 주니까." 그 기록은 `docs/evidence/pilot-notebook/`에 "파일럿(노트북) 기록"으로만 남습니다. 노트북 키 보관소 `platform-bao` 컨테이너도 플랫폼으로 옮기는 동안만 썼고 지금은 없습니다.

### 10.3 노트북 임시 링크(D-16 전)와 노트북 Executor

- 노트북 `kubectl port-forward`(`deploy/cloud/link.sh`)가 링크를 대신했습니다. 포트: 출발지 DB 15432(Executor 제어) · 15433(정방향 복제) · 15434(목적지 앱 경로), 목적지 DB 25432, PgBouncer 16432 · 26432, `chat-admin` 18081 · 28081, 역방향 relay 에이전트 17070, OpenBao 18200.
- 목적지 PgBouncer `target-src.ini`가 `host=host.lima.internal port=15434`(노트북 포워드)를 가리켰습니다. 22:19 KST에 `src-db-link`(D-16)로 바뀌었습니다(`docs/evidence/cloud-runs/cloud-executor0-checks-20261102T221434/target-after-d16-switch.jsonl`).
- 노트북 Executor(`deploy/cloud/executor.sh`, `http://127.0.0.1:8085`)를 쓰던 때가 있었습니다. 지금 Executor는 `executor-0` 하나입니다.
- OpenBao가 노트북 127.0.0.1에만 열려 있던 때, ESO 연결은 "링크가 선 뒤"로 미뤘습니다. 지금 상태는 4절입니다.

### 10.4 앞단의 지난 상태

- 2026-11-02 20:4x: 대표 이름은 활성 환경 Tunnel을 가리키는 CNAME이었고, 가중치 전환은 "코드만, 미실행"(감사 B21)이었습니다.
- 20:5x: LB를 켰지만 src pool이 비정상(530, 커넥터 없음)이라 대표 이름이 530이었습니다. `bravo-platform` Tunnel에도 커넥터가 없어 `b-free`가 로그인 뒤 530이었습니다.
- 20:5x Access 정책은 팀장 이메일 하나만 허용했습니다. 지금은 0.4절의 두 신원입니다.
