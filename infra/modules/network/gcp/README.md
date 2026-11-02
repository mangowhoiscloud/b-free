# network/gcp

GKE용 VPC를 만듭니다. 제안 D-01(GCP 목적지)에서 씁니다.

- 노드 서브넷 하나에 Pod와 Service 보조 대역을 둡니다. 대역은 `docs/architecture/01-environments.md` 4절입니다.
- 노드는 공인 IP가 없습니다. 아웃바운드는 Cloud NAT로만 나갑니다.
- Private Google Access를 켭니다. Google API는 NAT를 거치지 않습니다.
- Flow Logs는 변수로 켭니다. 기본값은 끔입니다.
- `proxy_only_cidr`를 주면 프록시 전용 서브넷(`REGIONAL_MANAGED_PROXY`)을 만듭니다. GKE Gateway의 내부 지역 부하 분산기(`gke-l7-rilb`)가 씁니다.

## 출력 계약

`network/aws`와 같은 이름: `vpc_id`, `cidr`, `azs`, `private_subnet_ids`.
GKE 입력용으로 `network_self_link`, `subnet_self_link`, `pod_range_name`, `service_range_name`을 더 둡니다.
