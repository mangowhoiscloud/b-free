# cluster/gke

GKE Standard 클러스터와 노드 풀을 만듭니다(D-21, TECH-STACK-SPEC 1절). `envs/target-gcp`가 씁니다.

## 왜 Standard인가

- Dataplane V2는 생성 때만 정할 수 있어 `datapath_provider = "ADVANCED_DATAPATH"`로 고정합니다.
- Autopilot은 kube-bench용 특권 Pod를 막아 통제 비교(T5, T14)를 할 수 없습니다(TECH-STACK-SPEC 2.1).
- 노드 풀은 이 모듈 안에 둡니다(`node` 모듈 없음, `infra/README.md`). 고객 입력은 크기뿐이고 머신 종류는 env 프리셋이 고릅니다.

## 무엇을 만드나

- 기본 노드 풀 없음(`initial_node_count = 0`, 1.36.4-gke.1469000+). `node_pools` 입력마다 `google_container_node_pool`.
- 사설 노드, 사설 엔드포인트(허가 대역 비움), IAM으로 지키는 DNS 엔드포인트. kubeconfig는 비밀 없이 `gke-gcloud-auth-plugin`을 부릅니다.
- 릴리스 채널 `REGULAR`, 최소 판 1.36(출발지 EKS 1.36, 온프렘 K3s 1.36과 같음).
- Gateway API(`CHANNEL_STANDARD`), Workload Identity(노드 `GKE_METADATA`), 관리형 Hubble relay.
- 고객 관리 키 둘: Secret 애플리케이션 계층 암호화, 노드 부팅 디스크와 PV. Cloud KMS 키링은 지울 수 없어 이름에 무작위 접미사를 붙입니다.
- 로그 · 지표는 시스템 구성요소만(비용). 관리 활동 감사 로그는 GKE가 늘 Cloud Logging에 씁니다(AUD-01).
- Arm 노드 taint: `architecture_taint_behavior`(기본 `NONE`). 모든 노드가 Arm인 클러스터에서 차트마다 toleration을 넣지 않게 합니다.

## 출력 계약

`cluster/eks`와 같은 이름(`name`, `kubernetes_version`, `api_endpoint`, `kubeconfig_path`, `cni`, `node_pools`, `storage_platform`, `addon_delivery`)에 GKE 것을 더합니다: `location`, `node_service_account`, `workload_pool`, `pd_kms_key_id`. `cni`는 `{ name = "gke-dataplane-v2", network_policy = true, encapsulation = "none", mtu = 1460 }`입니다.
