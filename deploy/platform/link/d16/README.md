# D-16 링크(플랫폼 쪽)

`cloudflared access tcp`로 상대 환경의 서비스를 플랫폼 안 로컬 이름으로 받습니다(D-16 제안, `docs/architecture/02-network.md` 4.3). Zero Trust(Access)를 켜면 임시 링크(D-16 전) `deploy/platform/link/temporary/`를 이것으로 바꿉니다. **지금은 적용하지 않습니다.** Zero Trust가 꺼져 있어 Access 앱과 링크 호스트가 없기 때문입니다.

| Service(ns `bravo-link`) | 포트 | 링크 호스트 | 서비스 토큰(OpenBao) |
|---|---|---|---|
| `src-k8s` | 443 | `t1-src-k8s.mangowhois.ai` | `secret/envs/platform/cf-link-src-k8s` |
| `src-db` | 5432 | `t1-src-db.mangowhois.ai` | `secret/envs/platform/cf-link-src-db` |
| `src-pgb` | 6432 | `t1-src-pgb.mangowhois.ai` | `secret/envs/platform/cf-link-src-pgb` |
| `dst-k8s` | 443 | `t1-dst-k8s.mangowhois.ai` | `secret/envs/platform/cf-link-dst-k8s` |
| `dst-db` | 5432 | `t1-dst-db.mangowhois.ai` | `secret/envs/platform/cf-link-dst-db` |
| `dst-pgb` | 6432 | `t1-dst-pgb.mangowhois.ai` | `secret/envs/platform/cf-link-dst-pgb` |

Service 이름은 임시 링크와 같습니다. 그래서 바꿀 때 소비자(수집기, Executor, 게이트, 리허설 서비스)의 설정이 그대로입니다. `*-k8s`의 kubeconfig는 `tls-server-name: kubernetes.default.svc`로 API 서버 인증서를 끝에서 끝으로 확인합니다.

## 켜는 순서(게이트: Zero Trust)

1. 사용자가 Cloudflare Zero Trust(Access)를 켭니다. 이 단계 전에는 아래 어느 것도 하지 않습니다.
2. 플랫폼 키 단계가 서비스 토큰 9개(`plat-src-k8s`, `plat-src-db`, `dst-src-db`, `plat-src-pgb`, `plat-dst-k8s`, `plat-dst-db`, `src-dst-db`, `plat-dst-pgb`, D-22가 정해지면 `src-dst-s3`)를 Cloudflare API로 만들고 값을 플랫폼 OpenBao에만 씁니다(`keys.sh issue-service-token`). 토큰 ID(비밀 아님)만 꺼냅니다.
3. `infra/envs/edge/terraform.tfvars`에 `zero_trust_enabled = true`와 `link_service_token_ids`를 넣고 apply합니다. 관리 Tunnel, Access 앱, Service Auth 정책이 먼저, DNS 레코드가 나중에 생깁니다(`infra/modules/edge_link/cloudflare`).
4. 관리 Tunnel 토큰을 노출 환경의 `cf-edge/cf-mgmt-tunnel`(키 `token`)에, 클라이언트 토큰을 이 네임스페이스의 `cf-link-<env>-<svc>`(키 `client_id`, `client_secret`)에 파이프로 전달합니다. 파일이나 인자로 남기지 않습니다.
5. 노출 환경에 `deploy/platform/link/customer-mgmt-tunnel/overlays/<env>`를 적용합니다.
6. `kubectl apply -k deploy/platform/link/d16`, 링크가 Ready인지 확인한 뒤 `deploy/platform/link/temporary/`를 지웁니다. 출발지 EKS 허용 목록의 플랫폼 NAT IP도 뺍니다.

## 판과 남은 확인

- 이미지 `cloudflare/cloudflared:2026.10.7`, digest `sha256:4955524aeaa379e154023d5a027c250f7b35041a2e7e950cf13f144bc9b9c5ba`(linux/amd64, linux/arm64 둘 다 있음, 2026-11-02 확인). N12 통과 판으로 다시 고정합니다(02-network.md 4.3).
- 2026.10.x의 `access tcp`는 서비스 토큰을 플래그나 환경 변수로만 받습니다(파일 옵션 없음). 설계는 파일 마운트를 권했지만 이 판에서는 Secret 환경 변수로 받고, 회전하면 Pod를 다시 띄웁니다.
- 앱 없는 라우트 · 정책 없는 앱에 닿지 않는지(N3), 링크 경유 지연(N11)은 Zero Trust를 켠 뒤 잽니다.
