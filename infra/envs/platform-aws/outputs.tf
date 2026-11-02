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

output "pod_identity_roles" {
  description = "Pod Identity 역할(namespace/serviceaccount → 역할 ARN)"
  value       = { for k, v in local.pod_roles : "${v.namespace}/${v.service_account}" => aws_iam_role.pod[k].arn }
}

output "source_access_groups" {
  description = "출발지 EKS access entry가 주는 K8s 그룹(역할 → 그룹)"
  value       = local.source_access
}

output "nat_gateway_id" {
  description = "플랫폼 Regional NAT. 출발지 EKS 허용 목록에 넣을 공인 IP는 이 NAT의 주소(임시 링크(D-16 전))"
  value       = module.network.nat_gateway_id
}

output "api_endpoint" {
  description = "EKS API 엔드포인트"
  value       = module.cluster.api_endpoint
}

output "gitops_repo_url" {
  description = "Argo CD가 당겨 갈 저장소"
  value       = var.gitops_repo_url
}
