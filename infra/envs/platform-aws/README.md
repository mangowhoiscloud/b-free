# envs/platform-aws

플랫폼(중앙 제어) 환경입니다. 스펙 §7.2 · T1 · T7: VPC ②에 EC2 1대 + RKE2 단일 노드. 류지환 결정(2026-11-02 21:1x)으로 같은 날 세운 임시 플랫폼 EKS는 지웠습니다(`terraform destroy -target`, 자원 42개). VPC ②는 남깁니다.

- VPC `10.20.0.0/16`, 사설 서브넷 셋, Regional NAT 하나(노드가 AZ a 하나라 활성 주소 하나).
- 노드: `modules/node/aws`. Ubuntu 24.04 arm64, `t4g.xlarge`, 공인 IP 없음, 인바운드 없음, IMDSv2 hop 1, 루트 gp3 60 GiB(노드 KMS 키). 운영자 접근은 SSM.
- 노드 준비: 공통 RKE2 cloud-init(`modules/cluster/onprem/cloud-init`, RKE2 v1.36.5+rke2r1, `profile: cis`)에 플랫폼 drop-in 하나(`cni: none`, 서비스 계정 발급자, provider-id, `rke2-traefik-crd` 끔). cloud-init이 바뀌어도 노드를 저절로 바꾸지 않습니다(상태가 있음): `terraform apply -replace=module.node.aws_instance.node`.
- 플랫폼 Pod의 AWS 권한: 서비스 계정 웹 신원. 발급자 = S3 공개 discovery 문서(`sa_oidc_issuer`, JWKS는 `deploy/platform/scripts/publish-sa-jwks.sh`), IAM OIDC 공급자, 역할 `collector-ro`(수집, 읽기) · `executor-ctl`(Executor, 제어) · `ebs-csi` · `ecr-pull`. 노드 역할은 SSM과 ECR 읽기뿐.
- 출발지 EKS access entry: `collector-ro` → 그룹 `bravo-collector`, `executor-ctl` → 그룹 `bravo-executor`(`deploy/platform/link/source-rbac`).
- ECR `bravo-platform/*`(태그 불변, 스캔), GitHub Actions OIDC 역할(`ci_identity`, `repo:mangowhoiscloud/b-free:ref:refs/heads/main`, ECR push만).
- 모든 자원 `component=platform`(provider `default_tags`).

```
terraform -chdir=infra/envs/platform-aws init -backend-config=backend.hcl
terraform -chdir=infra/envs/platform-aws apply
```
