output "vpc_id" {
  description = "VPC 네트워크 ID"
  value       = google_compute_network.this.id
}

output "cidr" {
  description = "노드 서브넷 대역"
  value       = var.node_cidr
}

output "azs" {
  description = "GCP는 리전 서브넷이라 리전 이름 하나"
  value       = [var.region]
}

output "private_subnet_ids" {
  description = "노드 서브넷 ID"
  value       = [google_compute_subnetwork.nodes.id]
}

output "network_self_link" {
  description = "GKE 입력용 네트워크 self link"
  value       = google_compute_network.this.self_link
}

output "subnet_self_link" {
  description = "GKE 입력용 서브넷 self link"
  value       = google_compute_subnetwork.nodes.self_link
}

output "pod_range_name" {
  description = "Pod 보조 대역 이름"
  value       = local.pod_range_name
}

output "service_range_name" {
  description = "Service 보조 대역 이름"
  value       = local.service_range_name
}

output "proxy_only_cidr" {
  description = "프록시 전용 서브넷 대역(없으면 null)"
  value       = var.proxy_only_cidr
}

output "subnet_name" {
  description = "노드 서브넷 이름"
  value       = google_compute_subnetwork.nodes.name
}
