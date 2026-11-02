# deploy/platform

플랫폼(우리 제어면) 묶음입니다. 스펙 §7.2 · T7대로 VPC ②의 EC2 1대 + RKE2 단일 노드(`infra/envs/platform-aws`, `infra/modules/node/aws`) 위에서 돕니다. 류지환 결정(2026-11-02 21:1x, "스펙 그대로 전부")으로 같은 날 20:3x에 세운 임시 플랫폼 EKS는 옮긴 뒤 지웠습니다(VPC ②는 그대로).

## 접근

| 무엇 | 어떻게 |
|---|---|
| kube API | SSM 포트 포워딩만. `aws ssm start-session --target <rke2_node.instance_id> --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["6443"],"localPortNumber":["26443"]}'`, 컨텍스트 `bravo-platform-rke2`(관리자 kubeconfig는 `~/.bravo/custody/platform-rke2/kubeconfig`, 0600). 16443은 온프렘 Lima 포워드가 씀 |
| OpenBao | `deploy/pilot/scripts/keys.sh`(기본 대상 = custody의 `bao_target`, 지금 `platform-rke2`). 포드로 kubectl port-forward. Shamir 3/2 몫은 그대로 custody |
| 콘솔 · API · MCP · Grafana | `https://b-free.mangowhois.ai`(Cloudflare Access 뒤, `/v1` API, `/mcp` MCP, `/grafana/` Grafana) |

## 묶음

| 폴더 | 무엇 |
|---|---|
| `gateway-api/` | Gateway API CRD v1.6.3(standard) |
| `../landing-zone/overlays/rke2/cilium-values.yaml` | Cilium 1.20.4 + Hubble, kube-proxy replacement(RKE2 `cni: none`, 랜딩 존과 같은 값) |
| `ebs-csi/` | EBS CSI 1.67.2. 자격은 서비스 계정 웹 신원(역할 `ebs-csi`) |
| `storage/` | StorageClass `bravo-db`, `bravo-general`(gp3, 노드 KMS 키) |
| `openbao/` | 플랫폼 OpenBao 2.7.2(raft 1, TLS, 감사 파일). 파일럿 → 임시 EKS → RKE2로 raft 스냅숏 두 번(`keys.sh migrate-to-platform`) |
| `control/` | 플랫폼 DB(CNPG 1), API · operator-mcp · 콘솔 · 게이트 · 수집기 · 리허설 · Executor, 마이그레이션 Job. 이미지는 플랫폼 ECR digest |
| `ecr-pull/` | ECR 당기기 토큰(12시간) 갱신 CronJob. 역할 `ecr-pull`(웹 신원) |
| `observability/` | OTel Collector, Prometheus, Tempo, Loki, Grafana |
| `edge/` | Gateway `bravo-gateway`(Cilium 구현, NodePort, 클라우드 LB 없음), HTTPRoute, 플랫폼 터널 커넥터 `cf-edge/cloudflared` |
| `link/d16/` | 플랫폼이 쓰는 D-16 링크 8개(`src-`/`dst-` × `db`, `pgb`, `k8s`, `admin`): `cloudflared access tcp` + Access 서비스 토큰 |
| `link/customer-mgmt-tunnel/` | 출발지 · 온프렘에 까는 관리 Tunnel 커넥터(`t1-src-mgmt`, `t1-dst-mgmt`) |
| `link/gke-consumer/` | GKE 목적지가 쓰는 링크(OpenBao, OTel, 출발지 DB). 노트북 relay 대체 |
| `link/source-rbac/`, `link/onprem-rbac/` | 플랫폼 신원의 K8s 권한(수집 읽기 / Executor ns chat exec / 리허설 rehearsal-*·verify-* 한정, VAP) |
| `register-envs/` | 환경 등록 Job(운영자 첫 로그인 뒤) |
| `kube-bench/` | rke2-cis-1.9 실행(근사, 결과 `docs/evidence/kube-bench/`) |
| `argocd/` | Argo CD 3.5.6 값과 app-of-apps. 비공개 저장소 자격이 없어 아직 동기화 못 함 |
| `envoy-gateway/`, `cilium/values.yaml`, `images/link/` | 임시 EKS 때만 쓴 기록(쓰지 않음) |

## 비밀(D-26)

모든 값은 플랫폼 OpenBao에만 있고, `scripts/deliver-secrets.sh deliver`가 파이프로만 Secret을 만듭니다. `scripts/onprem-tokens.sh`(온프렘 TokenRequest 168h), `scripts/gke-links.sh`(GKE 링크), `scripts/cluster-cas.sh`(공개 CA), `scripts/publish-sa-jwks.sh`(서비스 계정 JWKS를 S3 discovery에)도 같은 규칙입니다.

## 첫 설치 순서(실제로 한 순서)

1. `terraform -chdir=infra/envs/platform-aws apply`(노드, OIDC discovery, 웹 신원 역할, ECR, CI 역할)
2. SSM 포트 포워딩, kubeconfig를 custody로, `scripts/publish-sa-jwks.sh`
3. `gateway-api/` CRD, Cilium(랜딩 존 rke2 값), `ebs-csi/`, `storage/`, CNPG 운영자, `openbao/` → `keys.sh --target platform-rke2 tls` → `keys.sh migrate-to-platform --from <이전> --to platform-rke2` → `keys.sh k8s-auth-setup`
4. `scripts/deliver-secrets.sh generate|deliver`, `scripts/onprem-tokens.sh`, `scripts/cluster-cas.sh`
5. `observability/`, `control/`(`kubectl kustomize --load-restrictor LoadRestrictionsNone`), `link/d16/`, `edge/`
6. Argo CD 설치와 `argocd/root.yaml`. 저장소 자격(`secret/envs/platform/argocd-repo`)이 생기면 Git에서 당겨 옴
