# infra

스펙 §8.8의 구조를 따릅니다. 클러스터 종류는 팀 리드 결정(2026-11-02)을 따릅니다. RKE2를 모든 환경에서 빼고, 플랫폼과 출발지는 EKS, 온프렘 목적지는 K3s, GCP 목적지는 GKE입니다. CNI와 CSI는 `docs/architecture/09-cni-csi.md`가 정합니다. 모듈은 "기능 계약 + 환경별 구현"입니다. 환경 폴더는 모듈을 조합해 실제 자원을 만듭니다. 담당은 인프라 · CI 영역(T1, T2, T6, T7)입니다. 보안 구성요소(T9)는 보안 통제 · 게이트 영역입니다.

네트워크와 환경 사이 연결은 제안 D-16(`docs/architecture/02-network.md`)을 따릅니다. 근거는 `research/platform-research/network-design-2026-11-02.md`입니다.

## 모듈

| 모듈 | 구현 | 계약(출력) | 작업 |
|---|---|---|---|
| `network` | `aws/`, `gcp/`(D-01). 온프렘 VM 네트워크는 `cluster/k3s`가 Lima로 만듦 | VPC 네트워크, 대역. AWS는 사설 서브넷만 두고 VPC마다 Regional NAT 하나(제안 D-16). 공개 서브넷, 노드 공인 IP, VPC 사이 경로 없음. 피어링은 비상 토글 변수(기본 꺼짐). 보안 그룹은 인바운드 0(자기 SG 안 클러스터 통신만), 아웃바운드 443 · 7844 · 53 | T1 |
| `cluster` | `eks/`(플랫폼, 출발지), `gke/`(GCP 목적지, Autopilot 지역형), `k3s/`(온프렘 목적지, Lima VM: server 1대와 내장 etcd, agent 1대, VM마다 두 번째 디스크) | `name`, `kubernetes_version`, `api_endpoint`, `kubeconfig_path`, `cni`, `node_pools`, `storage_platform`(`eks`, `gke`, `k3s`), `addon_delivery`(`argocd`, `k3s-manifests`). 노드 풀은 이 모듈 안에 둠. EKS 노드 그룹은 IMDSv2 필수, hop limit 1 | T2, T6, T7 |
| `node` | 쓰지 않음(팀 리드 결정 2026-11-02). 노드는 `cluster` 구현 안의 EKS 관리형 노드 그룹, GKE Autopilot, Lima VM으로 만듦. 역할(`core`, `agents`)과 taint는 `cluster`의 `node_pools` 출력 | - |
| `storage` | 하나(입력 `platform`: `eks`, `gke`, `k3s`) | StorageClass `bravo-db`, `bravo-general`(모든 환경 같은 이름), 매니페스트, 필요한 차트(K3s는 TopoLVM). EBS는 gp3 + KMS, PD는 pd-balanced + CMEK, K3s는 VG `bravo-vg`. 할당량과 경보 규칙(`09` 5절) | T2, T6, T7 |
| `policy` | 하나(모든 클러스터 같음) | Kyverno 차트와 기본선 정책. 라벨 `bravo.io/tier`가 `rehearsal`, `verify`인 네임스페이스에 기본 거부와 DNS 허용 NetworkPolicy를 만들고 삭제를 막음(`09` 2절) | T3, T7 |
| `addons` | 하나(입력 전달 방식) | K3s는 `/var/lib/rancher/k3s/server/manifests`에 넣는 HelmChart 매니페스트. EKS, GKE는 Argo CD Application 파일만 만듦(클러스터에 적용하지 않음). 모든 차트와 이미지 판을 고정 | T6, T7 |
| `database` | `cnpg/`(모든 환경 공통) | 클러스터 이름, publication, 역할 5개(`docs/architecture/02-network.md` 5절). 역할은 마이그레이션 SQL로 만듦 | T6, T8 |
| `secret` | `aws/`(Secrets Manager + ESO), `onprem/`(OpenBao + ESO) | 앱이 받는 Secret 이름, 정책 | T6, T9 |
| `object_store` | `aws/`(S3), `onprem/`(SeaweedFS) | 버킷 이름, 엔드포인트, 앱 전용 자격 | T6, T9 |
| `edge` | `cloudflare/`(LB + 사용자 진입 Tunnel) | pool ID, 가중치 변경용 토큰 범위 | T17 |
| `edge_link` | `cloudflare/` | 테넌트와 환경마다 비밀이 아닌 것만 만들고 지움: 관리 Tunnel(구성은 `config_src = "local"`), Access 앱, Service Auth 정책, 호스트 DNS 레코드. Access 앱과 정책을 DNS 레코드보다 먼저 만들고 나중에 지움. 서비스 토큰과 Tunnel 토큰은 만들지 않고, 토큰 ID(비밀 아님)만 입력으로 받음. 출력은 링크 호스트 이름과 토큰의 비밀 저장소 경로(`tenants/<tenant_id>/…`). 제안 D-16 | T1(주인, 인프라 · CI). Access 정책 내용은 T17이 검토 |
| `ci_identity` | `aws/`(GitHub OIDC), `gcp/`(Workload Identity, D-01) | 공급자 중립 출력 `deploy_identity`(AWS는 역할 ARN, GCP는 서비스 계정) | T7 |

같은 모듈의 구현끼리는 출력 이름이 같아야 합니다. 그래야 환경 폴더가 구현만 바꿔 끼웁니다.

## 환경

| 폴더 | 만드는 것 | 상태 저장 |
|---|---|---|
| `envs/platform-aws/` | VPC ②(사설 서브넷 3개, Regional NAT), EKS(3 AZ, 노드 그룹 core와 agents, 관리형 add-on: VPC CNI 네트워크 정책, EBS CSI와 Pod Identity, CoreDNS, kube-proxy, Pod Identity Agent), `storage`, `policy`, `addons`, OIDC, 첫 설치용 CodeBuild 프로젝트(VPC 안, D-16). 배포 runner는 폴백일 때만(D-16) | S3 백엔드 + 잠금 |
| `envs/source-aws/` | VPC ①(사설 서브넷 2개, Regional NAT), EKS(API 비공개 전용), CNPG, PgBouncer, ESO, S3, KMS, `edge_link`, 첫 설치용 CodeBuild 프로젝트(VPC 안)(D-16) | 같음 |
| `envs/target-onprem/` | Lima VM 2대 위 K3s(CIS 강화 가이드 플래그), TopoLVM, Kyverno 기본선. 이후 CNPG, OpenBao, ESO, SeaweedFS, `edge_link`(D-16). 로컬에서 실제로 올리고 내림 | 같음(T1 전에는 로컬 상태) |
| `envs/target-gcp/` | GCP VPC와 Cloud NAT, GKE Autopilot(지역형, Dataplane V2, PD CSI, Workload Identity), `storage`, `policy`, `addons`(D-01). validate만 함 | 같음 |

환경 입력은 `name`, `region`, `size`(small, medium)뿐입니다. 고객이 Kubernetes를 몰라도 되게 CNI 플래그와 StorageClass 값은 모듈 안 기본값으로 둡니다.

K3s 노드 준비는 `cluster/k3s`가 맡습니다. 스펙 §8.8의 공통 cloud-init 자리입니다.
- K3s 바이너리를 판과 SHA-256으로 고정해 받습니다. 판을 고정하지 않은 `curl | sh` 설치는 쓰지 않습니다.
- K3s CIS 강화 가이드의 플래그를 겁니다: 비밀 암호화, PSA 설정 파일, 감사 로그, `protect-kernel-defaults`와 그에 맞는 sysctl.
- 감사 정책이 없으면 AUD-01 시험이 뜻을 갖지 않습니다.
- 스펙 §8.8의 Tailscale 설치는 제안 D-16에서 빠집니다. `cloudflared`는 노드가 아니라 클러스터 안 Deployment로 돕니다.

## 올리고 내리기

준비물은 Terraform 1.5.7 이상, kubectl, 온프렘이면 Lima `limactl` 1.2.1 이상입니다. 코드는 Terraform 1.5.7(마지막 MPL 판)에서 돌게 씁니다.

온프렘 목적지는 로컬 Mac의 Lima VM이라 비용이 없습니다. 명령 하나로 올리고 내립니다.

```sh
terraform -chdir=infra/envs/target-onprem init
terraform -chdir=infra/envs/target-onprem apply
export KUBECONFIG="$(terraform -chdir=infra/envs/target-onprem output -raw kubeconfig_path)"
infra/envs/target-onprem/scripts/verify.sh
terraform -chdir=infra/envs/target-onprem destroy
```

- `verify.sh`는 항목마다 PASS나 FAIL을 냅니다. 항목은 `docs/architecture/09-cni-csi.md` 8절에 있습니다.
- destroy는 VM과 두 번째 디스크를 함께 지웁니다.

클라우드 환경은 문법과 참조 검사만 합니다. apply는 팀 승인 뒤에만 합니다.

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
- 비밀은 Terraform이 만들지 않습니다. DB 비밀번호는 CNPG와 ESO가 클러스터 안에서 만들어 상태 파일에 들어가지 않게 합니다(NFR-6).
- Cloudflare 토큰도 Terraform이 만들지 않습니다(D-16, 정함). Terraform은 Tunnel, Access 앱, 정책처럼 비밀이 아닌 것만 만듭니다.
- 서비스 토큰과 Tunnel 토큰은 부트스트랩 스크립트가 Cloudflare API로 만듭니다. 스크립트는 값을 AWS Secrets Manager에 바로 씁니다. 상태 파일, 로컬 파일, 채팅에 남기지 않습니다. 스크립트 위치는 T1에서 정합니다.
- 온프렘 쪽 토큰(관리 Tunnel, 사용자 진입 Tunnel, `dst-src-db`)은 운영자가 같은 LAN에서 온프렘 OpenBao에 한 번 넣습니다. 절차는 런북에 적고, 넣은 사람, 시각, 폐기 일을 기록합니다.
- provider가 Tunnel을 만들 때 비밀을 상태에 남기는지는 T1에서 `terraform state show`로 확인합니다(확인 필요). 남으면 그 자원도 스크립트로 옮깁니다.
- 비공개 클러스터의 첫 설치는 VPC 안 CodeBuild가 한 번 합니다. Actions가 OIDC로 시작합니다. 끝나면 CodeBuild의 클러스터 접근 권한과 SG 규칙을 지웁니다. 그 뒤 배포는 Argo CD pull입니다(`docs/architecture/02-network.md` 14절).
- 빈 모듈 폴더는 Git에 남지 않습니다. 구현을 시작할 때 폴더마다 출력 계약을 적은 README를 둡니다.
