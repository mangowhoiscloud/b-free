# infra

스펙 §8.8의 구조를 따릅니다. 클러스터 종류는 팀장 결정을 따릅니다. 2026-11-02 21:10 KST에 RKE2를 스펙 r2대로 되돌렸습니다("스펙 그대로 전부", "RKE2 관련만", ADR 0010). 플랫폼은 VPC ②의 EC2 1대 + RKE2 단일 노드, 온프렘 목적지는 Ubuntu VM + RKE2(CIS), 둘 다 공통 cloud-init(`cloud-init/`, 아직 비어 있음)입니다. 출발지는 EKS, GCP 목적지는 GKE 그대로입니다. 아래 표의 EKS 플랫폼 · K3s 행은 결정 전 판이고, 그 판으로 만든 자원이 아직 떠 있습니다. CNI와 CSI는 `docs/architecture/09-cni-csi.md`가 정합니다. 모듈은 "기능 계약 + 환경별 구현"입니다. 환경 폴더는 모듈을 조합해 실제 자원을 만듭니다. 담당은 인프라 · CI 영역(T1, T2, T6, T7)입니다. 보안 구성요소(T9)는 보안 통제 · 게이트 영역입니다.

네트워크와 환경 사이 연결은 제안 D-16(`docs/architecture/02-network.md`)을 따릅니다. 근거는 `research/platform-research/network-design-2026-11-02.md`입니다.

## 모듈

| 모듈 | 구현 | 계약(출력) | 작업 |
|---|---|---|---|
| `network` | `aws/`, `gcp/`(D-01). 온프렘 VM 네트워크는 `cluster/k3s`가 Lima로 만듦 | VPC 네트워크, 대역. AWS는 사설 서브넷만 두고 VPC마다 Regional NAT 하나(제안 D-16). 공개 서브넷, 노드 공인 IP, VPC 사이 경로 없음. 피어링은 비상 토글 변수(기본 꺼짐). 보안 그룹은 인바운드 0(자기 SG 안 클러스터 통신만), 아웃바운드 443 · 7844 · 53 | T1 |
| `cluster` | `eks/`(플랫폼, 출발지), `gke/`(GCP 목적지, Standard + Dataplane V2, 노드 풀), `onprem/`(온프렘 목적지 K3s, Lima VM: server 1대와 내장 etcd, agent 1대, VM마다 두 번째 디스크) | `name`, `kubernetes_version`, `api_endpoint`, `kubeconfig_path`, `cni`, `node_pools`, `storage_platform`(`eks`, `gke`, `k3s`), `addon_delivery`(`argocd`, `k3s-manifests`). 노드 풀은 이 모듈 안에 둠. EKS 노드 그룹은 IMDSv2 필수, hop limit 1 | T2, T6, T7 |
| `node` | 쓰지 않음(팀 리드 결정 2026-11-02). 노드는 `cluster` 구현 안의 EKS 관리형 노드 그룹, GKE 노드 풀, Lima VM으로 만듦. 역할(`core`, `agents`)과 taint는 `cluster`의 `node_pools` 출력 | - |
| `storage` | 랜딩 존 overlay가 맡음(`deploy/landing-zone/overlays/*/storageclasses.yaml`) | StorageClass `bravo-db`, `bravo-general`(모든 환경 같은 이름). EBS는 gp3 + KMS, PD는 pd-balanced + CMEK. K3s 블록 저장은 결정 전이라 임시로 K3s 기본 `local-path`를 바탕으로 둠(D-22). TopoLVM은 쓰지 않음. 할당량과 경보 규칙(`09` 5절) | T2, T6, T7 |
| `policy` | 하나(모든 클러스터 같음) | Kyverno 차트와 기본선 정책(`GeneratingPolicy`, `policies.kyverno.io/v1`). 라벨 `bravo.io/tier`가 `rehearsal`, `verify`인 네임스페이스에 기본 거부와 DNS 허용 NetworkPolicy를 만들고 삭제를 막음(`09` 2절) | T3, T7 |
| `addons` | 하나(입력 전달 방식) | K3s는 `/var/lib/rancher/k3s/server/manifests`에 넣는 HelmChart 매니페스트. EKS, GKE는 Argo CD Application 파일만 만듦(클러스터에 적용하지 않음). 모든 차트와 이미지 판을 고정 | T6, T7 |
| `database` | `cnpg/`(모든 환경 공통) | 클러스터 이름, publication, 역할 5개(`docs/architecture/02-network.md` 5절). 역할은 마이그레이션 SQL로 만듦 | T6, T8 |
| `secret` | `aws/`(출발지 고객의 기존 Secrets Manager. S2 재료이자 이전 대상), `onprem/`(비어 있음) | 키와 비밀은 플랫폼 OpenBao 한 곳에만 둠(D-26). 고객 클러스터에는 금고를 두지 않고 ESO만 둠. 앱이 받는 Secret 이름은 모든 환경에서 같음 | T6, T9 |
| `object_store` | `aws/`(S3), `onprem/`(비어 있음. Rook-Ceph RGW 설치는 아직 없음, 랜딩 존 차트 판만 `versions.env`에 있음), GKE는 Cloud Storage | 같은 S3 계약 + 환경별 엔진(D-22). 버킷 이름, 엔드포인트, 앱 전용 자격 Secret 이름이 같음. SeaweedFS는 쓰지 않음 | T6, T9 |
| `edge` | `cloudflare/`(LB + 사용자 진입 Tunnel) | pool ID, 가중치 변경용 토큰 범위 | T17 |
| `edge_link` | `cloudflare/` | 테넌트와 환경마다 비밀이 아닌 것만 만들고 지움: 관리 Tunnel(구성은 `config_src = "local"`), Access 앱, Service Auth 정책, 호스트 DNS 레코드. Access 앱과 정책을 DNS 레코드보다 먼저 만들고 나중에 지움. 서비스 토큰과 Tunnel 토큰은 만들지 않고, 토큰 ID(비밀 아님)만 입력으로 받음. 출력은 링크 호스트 이름과 토큰의 비밀 저장소 경로(`tenants/<tenant_id>/…`). 제안 D-16 | T1(주인, 인프라 · CI). Access 정책 내용은 T17이 검토 |
| `ci_identity` | `aws/`(GitHub OIDC), `gcp/`(Workload Identity, D-01) | 공급자 중립 출력 `deploy_identity`(AWS는 역할 ARN, GCP는 서비스 계정) | T7 |

같은 모듈의 구현끼리는 출력 이름이 같아야 합니다. 그래야 환경 폴더가 구현만 바꿔 끼웁니다.

## 환경

| 폴더 | 만드는 것 | 상태 저장 |
|---|---|---|
| `envs/platform-aws/` | **결정 뒤: EC2 1대 + RKE2 단일 노드(T7, ADR 0010). 아래는 결정 전 EKS 판.** VPC ②(사설 서브넷 3개, Regional NAT), EKS(3 AZ, 노드 그룹 core와 agents, 관리형 add-on: VPC CNI 네트워크 정책, EBS CSI와 Pod Identity, CoreDNS, kube-proxy, Pod Identity Agent), `storage`, `policy`, `addons`, OIDC, 첫 설치용 CodeBuild 프로젝트(VPC 안, D-16). 배포 runner는 폴백일 때만(D-16) | S3 백엔드 + 잠금 |
| `envs/source-aws/` | VPC ①(사설 서브넷 2개, Regional NAT), EKS(API 비공개 전용), CNPG, PgBouncer, ESO, S3, KMS, `edge_link`, 첫 설치용 CodeBuild 프로젝트(VPC 안)(D-16) | 같음 |
| `envs/target-onprem/` | **2026-11-02 21:10 KST 결정으로 Ubuntu VM + RKE2(CIS) + 공통 cloud-init(T2, ADR 0010)으로 바뀜. 아래는 결정 전 K3s 판.** Lima VM 2대 위 K3s v1.36.5+k3s1(sha256 고정, CIS 강화 가이드 플래그)와 랜딩 존(`deploy/landing-zone/install.sh k3s`, 단계 cilium · base · images · cnpg · secrets · app · gateway): Cilium(VXLAN) + Hubble, Gateway API, StorageClass(임시 local-path), CNPG, 데모 앱, `cf-edge`. 비밀 값은 플랫폼 OpenBao에서 파이프로(D-26). Kyverno, ESO, Rook-Ceph RGW(D-22)는 설치 단계가 아직 없음(2026-11-02 20:45 KST). 로컬에서 실제로 올리고 내림 | 로컬 상태(`terraform.tfstate`, Git 제외) |
| `envs/target-gcp/` | GCP VPC와 Cloud NAT, GKE Standard(Dataplane V2, PD CSI, Workload Identity, 노드 풀), 랜딩 존 `gke` overlay(객체 저장은 Cloud Storage, D-22). 프로젝트 `b-free-target-20261102`. 명령 `envs/target-gcp/scripts/up.sh` · `verify.sh` · `down.sh` | S3 백엔드(`envs/target-gcp.tfstate`, 다른 환경과 같음. 이유는 `envs/target-gcp/README.md`) |

환경 입력은 `name`, `region`, `size`(small, medium)뿐입니다. 고객이 Kubernetes를 몰라도 되게 CNI 플래그와 StorageClass 값은 모듈 안 기본값으로 둡니다.

K3s 노드 준비는 `cluster/k3s`가 맡습니다. 스펙 §8.8의 공통 cloud-init 자리입니다.
- K3s 바이너리를 판과 SHA-256으로 고정해 받습니다. 판을 고정하지 않은 `curl | sh` 설치는 쓰지 않습니다.
- K3s CIS 강화 가이드의 플래그를 겁니다: 비밀 암호화, PSA 설정 파일, 감사 로그, `protect-kernel-defaults`와 그에 맞는 sysctl.
- 감사 정책이 없으면 AUD-01 시험이 뜻을 갖지 않습니다.
- 스펙 §8.8의 Tailscale 설치는 제안 D-16에서 빠집니다. `cloudflared`는 노드가 아니라 클러스터 안 Deployment로 돕니다.

## 올리고 내리기

준비물은 Terraform 1.17.1(모든 환경 `required_version = "~> 1.17.0"`, ADR 0008), kubectl, 온프렘이면 Lima `limactl`입니다. 설치는 `tools/toolchain/install-terraform.sh`(공식 릴리스 + SHA256 확인)입니다.

온프렘 목적지는 로컬 Mac의 Lima VM이라 비용이 없습니다. 명령 하나로 올리고 내립니다.

```sh
terraform -chdir=infra/envs/target-onprem init
terraform -chdir=infra/envs/target-onprem apply      # VM, K3s, kubeconfig 컨텍스트 bravo-onprem, 랜딩 존
kubectl --context "$(terraform -chdir=infra/envs/target-onprem output -raw kube_context)" get nodes
terraform -chdir=infra/envs/target-onprem destroy
```

- 시험 항목은 `docs/architecture/09-cni-csi.md` 8절에 있습니다. 그 항목을 돌리는 스크립트(옛 경로 `infra/envs/target-onprem/scripts/verify.sh`)는 아직 없습니다(감사 42).
- destroy는 VM과 두 번째 디스크를 함께 지웁니다.

클라우드 환경도 팀 승인 뒤 apply합니다. 2026-11-02 20:45 KST 기준 `source-aws`는 apply됨(EKS `bravo-source` ACTIVE), `platform-aws`와 `target-gcp`는 apply 진행 중입니다. 문법 검사만 할 때는 아래를 씁니다.

```sh
for env in platform-aws source-aws target-gcp; do
  terraform -chdir=infra/envs/$env init -backend=false
  terraform -chdir=infra/envs/$env validate
done
```

## 규칙

- 클라우드 환경(`platform-aws`, `source-aws`, `target-gcp`)의 `terraform apply`와 `destroy`는 팀 승인 뒤에만 합니다. 비용이 듭니다. 로컬 Lima 환경(`target-onprem`)은 비용이 없어 예외입니다.
- 모든 자원에 `component` 태그를 답니다(NFR-8). AWS는 provider `default_tags`로 강제합니다.
- 장기 키를 쓰지 않습니다. Actions는 OIDC로 역할을 받습니다(NFR-6).
- 상태 파일과 `.terraform/`은 커밋하지 않습니다(`.gitignore`). 상태 백엔드는 T1에서 가장 먼저 만듭니다.
- 비밀은 Terraform이 만들지 않습니다. DB 역할 비밀번호, 관리 토큰, 터널 토큰의 값은 플랫폼 OpenBao(`secret/envs/<env>/*`)에만 있습니다. 클러스터의 Secret은 플랫폼이 파이프로 만듭니다(`deploy/CLOUD-E2E-CONTRACT.md` 4 · 9절, D-26, NFR-6).
- Cloudflare 토큰도 Terraform이 만들지 않습니다(D-16, 정함). Terraform은 Tunnel, Access 앱, 정책처럼 비밀이 아닌 것만 만듭니다.
- 서비스 토큰과 Tunnel 토큰은 플랫폼 키 단계가 Cloudflare API에서 받아 플랫폼 OpenBao에 바로 씁니다(`deploy/pilot/scripts/keys.sh issue-tunnel-token <env> <tunnel_id>`, 경로 `secret/envs/<env>/cf-tunnel`). AWS Secrets Manager나 고객 클러스터의 금고에는 쓰지 않습니다(D-26, ADR 0007). 상태 파일, 로컬 파일, 채팅에 남기지 않습니다.
- 클러스터로 넘기는 일도 플랫폼이 합니다. 값은 파이프로만 `kubectl create secret ... --from-file=token=/dev/stdin`에 들어갑니다. 온프렘에 금고를 따로 두거나 운영자가 손으로 넣는 절차는 없습니다.
- provider가 Tunnel을 만들 때 비밀을 상태에 남기는지는 T1에서 `terraform state show`로 확인합니다(확인 필요). 남으면 그 자원도 스크립트로 옮깁니다.
- 비공개 클러스터의 첫 설치는 VPC 안 CodeBuild가 한 번 합니다. Actions가 OIDC로 시작합니다. 끝나면 CodeBuild의 클러스터 접근 권한과 SG 규칙을 지웁니다. 그 뒤 배포는 Argo CD pull입니다(`docs/architecture/02-network.md` 14절).
- 빈 모듈 폴더는 Git에 남지 않습니다. 구현을 시작할 때 폴더마다 출력 계약을 적은 README를 둡니다.
