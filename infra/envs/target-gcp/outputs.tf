output "cluster_name" {
  description = "GKE 클러스터 이름"
  value       = module.cluster.name
}

output "kubernetes_version" {
  description = "최소 K8s minor(GKE 릴리스 채널 안)"
  value       = module.cluster.kubernetes_version
}

output "kubeconfig_path" {
  description = "비밀 없는 kubeconfig 경로(DNS 엔드포인트, IAM 인증)"
  value       = module.cluster.kubeconfig_path
}

output "cni" {
  description = "CNI 요약"
  value       = module.cluster.cni
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

output "attachments_bucket" {
  description = "첨부 버킷(Cloud Storage). 앱은 S3 계약 환경 변수로 받음"
  value       = google_storage_bucket.attachments.name
}
