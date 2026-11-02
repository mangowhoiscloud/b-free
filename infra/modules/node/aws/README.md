# node/aws

스펙 §7.2와 T7의 "VPC ②에 EC2 1대 + RKE2 단일 노드"입니다(류지환 결정 2026-11-02 21:1x, D-21의 RKE2 부분을 스펙대로 되돌림).

- Ubuntu 24.04 arm64(Canonical 공개 SSM 파라미터), 사설 서브넷, 공인 IP 없음, 보안 그룹 인바운드 없음.
- IMDSv2 필수, hop limit 1. 루트 디스크 gp3, 이 모듈의 KMS 키로 암호화.
- 노드 역할: `AmazonSSMManagedInstanceCore`(운영자 SSM 포트 포워딩), `AmazonEC2ContainerRegistryPullOnly`(같은 계정 ECR digest 이미지). 플랫폼 Pod의 AWS 권한은 노드 역할이 아니라 서비스 계정 웹 신원(`infra/envs/platform-aws/rke2.tf`)으로 받습니다.
- `user_data`는 공통 RKE2 cloud-init(`infra/modules/cluster/onprem/cloud-init`)을 그대로 받습니다. 플랫폼만의 추가(서비스 계정 발급자)는 같은 문서에 MIME 부분 하나로 붙입니다.

| 출력 | 뜻 |
|---|---|
| `instance_id` | SSM 세션 대상 |
| `private_ip` | 사설 IP |
| `security_group_id` | 노드 SG |
| `node_role_name` | 노드 역할 이름 |
| `ami_id` | 실제 AMI |
