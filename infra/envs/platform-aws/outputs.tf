output "ecr_repositories" {
  description = "플랫폼 이미지 저장소(이미지 이름 → URL)"
  value       = { for k, r in aws_ecr_repository.platform : k => r.repository_url }
}

output "ci_deploy_identity" {
  description = "GitHub Actions OIDC 역할 ARN(role-to-assume)"
  value       = module.ci_identity.deploy_identity
}

output "ci_trusted_subject" {
  description = "CI 역할이 신뢰하는 OIDC sub 하나"
  value       = module.ci_identity.trusted_subject
}

output "nat_gateway_id" {
  description = "플랫폼 Regional NAT(VPC ②). 출발지 EKS API 허용 목록에 이 NAT 공인 IP가 있음(수집기 · Executor의 EKS 토큰 경로, 임시 링크(D-16 전))"
  value       = module.network.nat_gateway_id
}

output "gitops_repo_url" {
  description = "Argo CD가 당겨 갈 저장소"
  value       = var.gitops_repo_url
}

output "rke2_node" {
  description = "플랫폼 RKE2 단일 노드(SSM 세션 대상)"
  value = {
    instance_id = module.node.instance_id
    private_ip  = module.node.private_ip
    ami_id      = module.node.ami_id
    rke2        = local.rke2.version
  }
}

output "sa_oidc_issuer" {
  description = "RKE2 서비스 계정 발급자(S3 공개 discovery 문서). JWKS를 이 버킷에 올려야 웹 신원이 동작"
  value       = local.oidc_issuer
}

output "sa_oidc_bucket" {
  description = "discovery 문서 버킷"
  value       = aws_s3_bucket.oidc.bucket
}

output "web_identity_roles" {
  description = "서비스 계정 → 역할 ARN(AWS_ROLE_ARN)"
  value       = { for k, v in local.web_identity_roles : v.sa => aws_iam_role.web_identity[k].arn }
}

output "node_kms_key_arn" {
  description = "노드 · EBS CSI 볼륨 KMS 키(StorageClass kmsKeyId)"
  value       = module.node.kms_key_arn
}
