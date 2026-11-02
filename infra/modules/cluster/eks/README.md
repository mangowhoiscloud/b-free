# cluster/eks

Amazon EKS 클러스터를 만듭니다. `platform-aws`(중앙 제어)와 `source-aws`(출발지)가 같이 씁니다.

## 무엇을 만드나

- 제어면: API는 비공개 엔드포인트만 엽니다. 인증 모드는 access entry(API)입니다.
- Secret 봉투 암호화: 고객 관리 KMS 키를 씁니다. 제어면 로그는 `api`, `audit`, `authenticator`입니다.
- 지원 정책: `STANDARD`입니다. 표준 지원이 끝나도 확장 지원 요금으로 넘어가지 않습니다.
- 노드: 역할별 노드 풀을 AZ마다 관리형 노드 그룹 하나로 펼칩니다. EBS 볼륨과 노드의 AZ가 맞습니다.
- 노드 설정: 시작 템플릿에서 IMDSv2 필수, hop limit 1, 루트 디스크 gp3와 KMS 암호화를 겁니다.
- 관리형 add-on: `vpc-cni`(IPAM만), `kube-proxy`, `eks-pod-identity-agent`, `coredns`, `aws-ebs-csi-driver`, `snapshot-controller`.
- EBS CSI 권한: EKS Pod Identity로 컨트롤러에만 줍니다. 노드 역할에는 EBS 권한이 없습니다.
- EBS CSI의 KMS 정책: 이 클러스터의 EBS 키 하나에만, `kms:ViaService = ec2.<region>`일 때만 허용합니다.
- kubeconfig: 비밀이 없는 파일을 씁니다. 토큰은 `aws eks get-token`이 받습니다.

## 네트워크 정책과 Cilium chaining

- 기본 엔진은 Cilium입니다(`network_policy_engine = "cilium"`). Cilium과 Hubble은 landing zone 묶음이 깝니다. Terraform은 깔지 않습니다.
- chaining 모드에서는 VPC CNI가 ENI와 IPAM을 맡습니다. Cilium은 같은 veth에 eBPF를 붙여 정책, 부하 분산, Hubble을 맡습니다(Cilium 문서 "AWS VPC CNI plugin", 1.20.4, 2026-11-02 확인).
- 그래서 VPC CNI add-on은 `{"enableNetworkPolicy":"false"}`로 둡니다. 정책 엔진을 둘 두지 않으려는 선택입니다. Cilium 문서는 이 설정을 직접 요구하지 않습니다(확인). 두 엔진이 같은 veth에 eBPF를 붙이면 판정이 겹친다고 봅니다(추정).
- Cilium 값은 문서의 넷입니다: `cni.chainingMode=aws-cni`, `cni.exclusive=false`, `enableIPv4Masquerade=false`, `routingMode=native`. 값은 `deploy/landing-zone` eks overlay에 둡니다.
- Cilium을 깐 뒤 이미 떠 있던 Pod는 다시 띄워야 정책이 걸립니다(Cilium 문서). landing zone 첫 동기화 직후 한 번 합니다.
- chaining에서는 L7 정책과 IPsec이 제한됩니다(Cilium 문서). 우리 정책은 L3, L4만 씁니다.
- 폴백은 `network_policy_engine = "vpc-cni"`입니다. 이때 VPC CNI 정책 에이전트를 켜고 거부 기록을 남깁니다. 집행 모드는 add-on 기본값 `standard`입니다.

## 입력

| 변수 | 뜻 |
|---|---|
| `name`, `kubernetes_version` | 이름, K8s minor(기본 1.36) |
| `vpc_id`, `subnet_ids`, `subnet_ids_by_az` | `network/aws` 출력 |
| `service_cidr` | Service 대역. 명시 입력 |
| `node_pools` | 역할 → 인스턴스 종류, AZ, AZ당 최소 · 목표 · 최대, 라벨, taint |
| `addon_versions` | add-on 판 고정. 비우면 이 K8s 판의 기본 판 |
| `network_policy_engine` | `cilium`(기본, chaining) 또는 `vpc-cni`(폴백) |
| `admin_principal_arns` | 관리자 access entry. 생성자는 자동 |
| `kubeconfig_path` | kubeconfig 경로 |

## 출력 계약

`cluster/gke`, `cluster/k3s`와 이름이 같습니다.

| 출력 | 값 |
|---|---|
| `name`, `kubernetes_version`, `api_endpoint`, `kubeconfig_path` | 클러스터 기본 정보 |
| `cni` | `{ name = "cilium-aws-cni-chaining", network_policy = true, encapsulation = "none", mtu = 9001 }` |
| `node_pools` | 역할별 AZ, 인스턴스 종류, 아키텍처, taint |
| `storage_platform` | `"eks"`. `landing_zone`의 `provider_kind` |
| `addon_delivery` | `"argocd"`. `landing_zone`의 `delivery` |
| `ebs_kms_key_arn` | EBS 고객 관리 키 |
| `node_role_arn` | 노드 역할 |
