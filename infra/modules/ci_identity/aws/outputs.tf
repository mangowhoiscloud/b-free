# 공급자 중립 출력(infra/README.md 계약). GCP 구현은 같은 이름으로 서비스 계정을 냄.

output "deploy_identity" {
  description = "CI가 받는 배포 신원. AWS는 역할 ARN(aws-actions/configure-aws-credentials의 role-to-assume)"
  value       = aws_iam_role.deploy.arn
}

output "trusted_subject" {
  description = "신뢰하는 OIDC sub 값 하나"
  value       = local.subject
}

output "oidc_provider_arn" {
  description = "GitHub OIDC 공급자 ARN"
  value       = local.provider_arn
}
