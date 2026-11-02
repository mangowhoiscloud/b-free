# 비밀 값 없음(D-26). 스크립트(scripts/*.sh)와 landing zone gke overlay가 이 출력만 읽음.

output "project_id" {
  description = "GCP 프로젝트"
  value       = var.project_id
}

output "region" {
  description = "리전"
  value       = var.region
}

output "cluster_name" {
  description = "GKE 클러스터 이름"
  value       = module.cluster.name
}

output "cluster_location" {
  description = "클러스터 위치(존)"
  value       = module.cluster.location
}

output "kubernetes_version" {
  description = "최소 K8s minor(GKE 릴리스 채널 안)"
  value       = module.cluster.kubernetes_version
}

output "kubeconfig_path" {
  description = "비밀 없는 kubeconfig 경로(DNS 엔드포인트, IAM 인증)"
  value       = module.cluster.kubeconfig_path
}

output "api_endpoint" {
  description = "IAM으로 지키는 DNS 엔드포인트"
  value       = module.cluster.api_endpoint
}

output "cni" {
  description = "CNI 요약"
  value       = module.cluster.cni
}

output "node_pools" {
  description = "노드 풀"
  value       = module.cluster.node_pools
}

output "workload_pool" {
  description = "Workload Identity 풀"
  value       = module.cluster.workload_pool
}

output "pd_kms_key_id" {
  description = "PD · Hyperdisk CMEK(StorageClass bravo-db, bravo-general)"
  value       = module.cluster.pd_kms_key_id
}

output "image_registry" {
  description = "목적지 이미지 저장소 경로(Artifact Registry)"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "gateway_address_name" {
  description = "Gateway bravo-gateway의 고정 내부 주소 이름(NamedAddress)"
  value       = google_compute_address.gateway.name
}

output "gateway_address" {
  description = "Gateway 고정 내부 주소"
  value       = google_compute_address.gateway.address
}

output "proxy_only_cidr" {
  description = "프록시 전용 서브넷(Gateway 프록시가 Pod로 오는 출발지)"
  value       = module.network.proxy_only_cidr
}

output "attachments_bucket" {
  description = "첨부 버킷(Cloud Storage). 앱은 S3 계약 환경 변수로 받음"
  value       = google_storage_bucket.attachments.name
}

output "attachments_canary_key" {
  description = "게이트 OBJ 시험의 고정 키 시험 객체"
  value       = google_storage_bucket_object.canary.name
}

output "attachments_kms_key" {
  description = "첨부 버킷 기본 암호화 키(CMEK)"
  value       = google_kms_crypto_key.attachments.id
}

output "chat_app_service_account" {
  description = "앱 HMAC 키 주인(버킷 객체 읽기 · 쓰기만)"
  value       = google_service_account.chat_app.email
}

output "gate_other_service_account" {
  description = "OBJ-05 대조용 다른 자격(버킷 권한 없음)"
  value       = google_service_account.gate_other.email
}

output "storage_https_enforced" {
  description = "storage.secureHttpTransport 조직 정책을 걸었는지"
  value       = var.enforce_storage_https
}
