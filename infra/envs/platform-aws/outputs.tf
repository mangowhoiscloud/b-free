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

output "storage_classes" {
  description = "모든 환경에서 같은 StorageClass 이름"
  value       = module.landing_zone.storage_class_names
}

output "gitops_files" {
  description = "Argo CD가 당겨 갈 파일"
  value       = module.landing_zone.files
}

output "landing_zone_components" {
  description = "landing zone 구성요소와 차트 판(모든 고객 클러스터 공통)"
  value       = module.landing_zone.components
}
