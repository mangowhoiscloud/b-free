# network/aws

사설 서브넷만 있는 VPC를 만듭니다. 제안 D-16과 `docs/architecture/02-network.md` 2절을 따릅니다.

- 공개 서브넷과 노드 공인 IP가 없습니다.
- 아웃바운드는 VPC마다 Regional NAT Gateway 하나로 나갑니다. auto mode라서 EIP와 AZ 확장을 AWS가 맡습니다.
- Regional NAT는 aws provider 6.71의 `availability_mode = "regional"`로 만듭니다. IGW는 필요하고, 공개 서브넷은 필요 없습니다(provider 문서 예제, 2026-11-02 확인).
- 기본 보안 그룹은 규칙을 모두 비웁니다.
- S3 게이트웨이 엔드포인트를 둡니다. S3 트래픽이 NAT 데이터 처리 비용을 내지 않습니다.
- Flow Logs는 변수로 켭니다. 기본값은 끔입니다.

## 입력

| 변수 | 뜻 |
|---|---|
| `name` | 이름 접두어 |
| `cidr` | VPC 대역 |
| `azs`, `private_subnet_cidrs` | 가용 영역과 서브넷 대역. 순서가 같음 |
| `cluster_name` | EKS 내부 로드밸런서 발견 태그. 선택 |
| `enable_flow_logs` | Flow Logs. 기본 끔 |

## 출력 계약

`vpc_id`, `cidr`, `azs`, `private_subnet_ids`, `private_subnet_ids_by_az`, `nat_gateway_id`. `network/gcp`도 같은 뜻의 출력을 둡니다.
