# D-16 링크(플랫폼 쪽)

`cloudflared access tcp`로 상대 환경의 서비스를 플랫폼 안 로컬 이름으로 받습니다(D-16 제안, `docs/architecture/02-network.md` 4.3). 플랫폼 RKE2에 적용되어 있습니다(Argo CD 앱 `link-d16`). 2026-11-02 23:28 KST에 아래 표의 링크와 `src-admin` · `dst-admin` · `dst-s3`가 모두 2/2 Ready였습니다.

| Service(ns `bravo-link`) | 포트 | 링크 호스트 | 서비스 토큰(OpenBao) |
|---|---|---|---|
| `src-k8s` | 443 | `t1-src-k8s.mangowhois.ai` | `secret/envs/platform/cf-link-src-k8s` |
| `src-db` | 5432 | `t1-src-db.mangowhois.ai` | `secret/envs/platform/cf-link-src-db` |
| `src-pgb` | 6432 | `t1-src-pgb.mangowhois.ai` | `secret/envs/platform/cf-link-src-pgb` |
| `dst-k8s` | 443 | `t1-dst-k8s.mangowhois.ai` | `secret/envs/platform/cf-link-dst-k8s` |
| `dst-db` | 5432 | `t1-dst-db.mangowhois.ai` | `secret/envs/platform/cf-link-dst-db` |
| `dst-pgb` | 6432 | `t1-dst-pgb.mangowhois.ai` | `secret/envs/platform/cf-link-dst-pgb` |
| `dst-s3http` | 80 | `t1-dst-s3http.mangowhois.ai`(RGW 평문 수신 80 → Pod 8080) | `secret/envs/platform/cf-link-dst-s3http` |
| `gke-db` | 5432 | `t1-gke-db.mangowhois.ai`(GKE 관리 Tunnel `t1-gke-mgmt`) | `secret/envs/platform/cf-link-gke-db` |

`dst-s3http`와 `gke-db`는 2026-11-03 01:0x–01:1x KST에 더했습니다. 소비자는 플랫폼 게이트뿐입니다. `dst-s3http`는 OBJ-06의 평문 요청(TLS 전용 버킷 정책이 `AccessDenied`를 내야 함)만, `gke-db`는 GKE chat-db의 DB 질문(DB-*, DR-ARCHIVE)만 씁니다. 두 링크 클라이언트는 Argo CD 동기화 전에 같은 매니페스트로 먼저 적용했습니다.

Service 이름은 임시 링크와 같습니다. 그래서 바꿀 때 소비자(수집기, Executor, 게이트, 리허설 서비스)의 설정이 그대로입니다. `*-k8s`는 API 서버 인증서를 끝에서 끝으로 확인합니다. 링크는 TCP만 넘기고 TLS는 소비자와 API 서버 사이에서 맺습니다.

| 소비자 | 링크 | 인증 | TLS 이름 |
|---|---|---|---|
| 수집기(`GET /v1/clusters`, 스냅숏) | `src-k8s` | EKS IAM 토큰(웹 아이덴티티 역할, access entry `bravo-collector`) | 실제 EKS 엔드포인트 호스트(`eks:DescribeCluster`에서 읽음). 레지스트리 `deploy/platform/control/config/environments.yaml`의 `api_server` |
| Executor | `src-k8s` | EKS IAM 토큰(access entry `bravo-executor`) | `kubernetes.default.svc`(`deploy/cloud/in-cluster/kubeconfig.template.yaml`) |
| 수집기, 리허설 서비스, Executor | `dst-k8s` | 읽기 전용 · 리허설 · Executor SA 토큰(OpenBao에서 Secret으로) | `kubernetes.default.svc` |
| 게이트 | `src-k8s` | EKS IAM 토큰(웹 아이덴티티 역할 `gate-ro`, access entry `bravo-gate`) | 실제 EKS 엔드포인트 호스트(수집기와 같은 방식) |
| 게이트 | `dst-k8s` | SA `kube-system/bravo-gate` 토큰(OpenBao `secret/envs/target-onprem/k8s-gate`) | `kubernetes.default.svc` |
| 게이트 | `src-db`, `dst-db`, `gke-db` | DB 역할 `gate_ro`(비밀번호 OpenBao `secret/envs/<env>/gate-db`) | `chat-db-rw.chat.svc`(libpq `hostaddr`로 링크에 붙고 `verify-full`은 DB 이름으로) |
| 게이트 | `dst-s3`, `dst-s3http` | RGW 앱 키 · 권한 없는 `bravo-gate-other` 키(OpenBao) | `127.0.0.1`(게이트 사이드카 `s3-local`, RGW 인증서 IP SAN) |

`kubernetes.default.svc`와 EKS 엔드포인트 호스트는 둘 다 EKS API 인증서의 SAN에 있습니다(2026-11-02 확인, `docs/evidence/d16-source-k8s/`). 출발지 EKS 공개 엔드포인트 허용 목록에는 플랫폼 NAT IP가 없습니다. 플랫폼이 출발지 API에 닿는 길은 이 링크뿐입니다.

## 켜는 순서(게이트: Zero Trust, 2026-11-02에 끝냄)

1. 사용자가 Cloudflare Zero Trust(Access)를 켭니다. 이 단계 전에는 아래 어느 것도 하지 않습니다.
2. 플랫폼 키 단계가 서비스 토큰 9개(`plat-src-k8s`, `plat-src-db`, `dst-src-db`, `plat-src-pgb`, `plat-dst-k8s`, `plat-dst-db`, `src-dst-db`, `plat-dst-pgb`, D-22가 정해지면 `src-dst-s3`)를 Cloudflare API로 만들고 값을 플랫폼 OpenBao에만 씁니다(`keys.sh issue-service-token`). 토큰 ID(비밀 아님)만 꺼냅니다.
3. `infra/envs/edge/terraform.tfvars`에 `zero_trust_enabled = true`와 `link_service_token_ids`를 넣고 apply합니다. 관리 Tunnel, Access 앱, Service Auth 정책이 먼저, DNS 레코드가 나중에 생깁니다(`infra/modules/edge_link/cloudflare`).
4. 관리 Tunnel 토큰을 노출 환경의 `cf-edge/cf-mgmt-tunnel`(키 `token`)에, 클라이언트 토큰을 이 네임스페이스의 `cf-link-<env>-<svc>`(키 `client_id`, `client_secret`)에 파이프로 전달합니다. 파일이나 인자로 남기지 않습니다.
5. 노출 환경에 `deploy/platform/link/customer-mgmt-tunnel/overlays/<env>`를 적용합니다.
6. `kubectl apply -k deploy/platform/link/d16`, 링크가 Ready인지 확인한 뒤 `deploy/platform/link/temporary/`를 지웁니다. 출발지 EKS 허용 목록의 플랫폼 NAT IP도 뺍니다(2026-11-02 23:44 KST, `infra/envs/source-aws`의 `bootstrap_public_cidrs`).

## 판과 남은 확인

- 이미지 `cloudflare/cloudflared:2026.10.7`, digest `sha256:4955524aeaa379e154023d5a027c250f7b35041a2e7e950cf13f144bc9b9c5ba`(linux/amd64, linux/arm64 둘 다 있음, 2026-11-02 확인). N12 통과 판으로 다시 고정합니다(02-network.md 4.3).
- 2026.10.x의 `access tcp`는 서비스 토큰을 플래그나 환경 변수로만 받습니다(파일 옵션 없음). 설계는 파일 마운트를 권했지만 이 판에서는 Secret 환경 변수로 받고, 회전하면 Pod를 다시 띄웁니다.
- 앱 없는 라우트 · 정책 없는 앱에 닿지 않는지(N3), 링크 경유 지연(N11)은 Zero Trust를 켠 뒤 잽니다.
