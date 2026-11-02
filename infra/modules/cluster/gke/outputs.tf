# 출력 이름은 cluster/eks, cluster/k3s와 같음(infra/README.md 계약).

output "name" {
  description = "클러스터 이름"
  value       = google_container_cluster.this.name
}

output "kubernetes_version" {
  description = "최소 K8s minor(release_channel 안)"
  value       = var.kubernetes_version
}

output "api_endpoint" {
  description = "IAM으로 지키는 DNS 엔드포인트"
  value       = "https://${local.dns_endpoint}"
}

output "kubeconfig_path" {
  description = "비밀 없는 kubeconfig 경로"
  value       = local_file.kubeconfig.filename
}

output "cni" {
  description = "CNI 요약. Dataplane V2(eBPF)는 VPC 별칭 IP로 Pod를 바로 라우팅하므로 캡슐화가 없음"
  value = {
    name           = "gke-dataplane-v2"
    network_policy = true
    encapsulation  = "none"
    mtu            = 1460
  }
}

output "node_pools" {
  description = "Autopilot은 노드 풀을 Google이 관리함"
  value = {
    autopilot = {
      azs            = google_container_cluster.this.node_locations
      instance_types = []
      arch           = "managed"
      desired        = null
      taints         = []
    }
  }
}

output "storage_platform" {
  description = "landing_zone 모듈의 provider_kind 값"
  value       = "gke"
}

output "addon_delivery" {
  description = "landing_zone 모듈의 delivery 값. 클라우드는 Argo CD Application 파일"
  value       = "argocd"
}

output "pd_kms_key_id" {
  description = "PD 고객 관리 키. landing zone gke overlay의 bravo-db, bravo-general이 씀"
  value       = google_kms_crypto_key.pd.id
}
