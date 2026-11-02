output "cluster_name" {
  description = "EKS 클러스터 이름"
  value       = module.cluster.name
}

output "kubernetes_version" {
  description = "K8s minor"
  value       = module.cluster.kubernetes_version
}

output "kubeconfig_path" {
  description = "비밀 없는 kubeconfig 경로. API는 비공개라 VPC 안이나 링크로만 닿음"
  value       = module.cluster.kubeconfig_path
}

output "cni" {
  description = "CNI 요약"
  value       = module.cluster.cni
}

output "node_pools" {
  description = "역할별 노드 풀"
  value       = module.cluster.node_pools
}

output "attachments_bucket" {
  description = "첨부 S3 버킷 이름"
  value       = aws_s3_bucket.attachments.bucket
}

output "attachments_bucket_region" {
  description = "첨부 버킷 리전"
  value       = var.region
}

output "bucket_policy_mode" {
  description = "첨부 버킷 정책 모드(strong 또는 demo-weak)"
  value       = var.demo_weak_bucket_policy ? "demo-weak" : "strong"
}

output "db_secret_arn" {
  description = "앱 DB 비밀번호 비밀의 ARN. 값은 비어 있음(README 참고)"
  value       = aws_secretsmanager_secret.db_app.arn
}

output "app_kms_key_arn" {
  description = "앱 데이터 KMS 키"
  value       = aws_kms_key.app.arn
}

output "role_arns" {
  description = "EKS Pod Identity 역할(네임스페이스/서비스 계정 → 역할)"
  value = {
    "chat/chat-app"                     = aws_iam_role.chat_app.arn
    "external-secrets/external-secrets" = aws_iam_role.external_secrets.arn
  }
}

output "api_endpoint" {
  description = "EKS API 비공개 엔드포인트"
  value       = module.cluster.api_endpoint
}

output "storage_platform" {
  description = "landing zone provider_kind"
  value       = module.cluster.storage_platform
}

output "addon_delivery" {
  description = "landing zone delivery"
  value       = module.cluster.addon_delivery
}
