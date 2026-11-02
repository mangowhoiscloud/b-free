# GKE Autopilot 리전 클러스터. 노드 풀, 노드 OS, 업그레이드를 Google이 맡음.
# 초보 고객이 노드를 고르지 않아도 되고, Dataplane V2(NetworkPolicy 집행)와 PD CSI가 기본으로 켜짐.

resource "google_service_account" "nodes" {
  project      = var.project_id
  account_id   = substr("${var.name}-nodes", 0, 30)
  display_name = "${var.name} GKE nodes (least privilege)"
}

resource "google_project_iam_member" "nodes" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_container_cluster" "this" {
  project  = var.project_id
  name     = var.name
  location = var.region

  enable_autopilot    = true
  deletion_protection = var.deletion_protection

  network    = var.network_self_link
  subnetwork = var.subnet_self_link

  # Autopilot은 늘 Dataplane V2. 명시해서 NetworkPolicy 집행이 이 값에 기댄다는 것을 드러냄.
  datapath_provider = "ADVANCED_DATAPATH"

  min_master_version = var.kubernetes_version

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pod_range_name
    services_secondary_range_name = var.service_range_name
  }

  # 노드는 공인 IP가 없음. API는 VPC 안의 사설 IP와 IAM으로 지키는 DNS 엔드포인트로만 받음.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = true
  }

  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
  }

  database_encryption {
    state    = "ENCRYPTED"
    key_name = google_kms_crypto_key.secrets.id
  }

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account   = google_service_account.nodes.email
      boot_disk_kms_key = google_kms_crypto_key.pd.id
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = "18:00"
    }
  }

  resource_labels = var.labels

  depends_on = [
    google_kms_crypto_key_iam_member.secrets_gke,
    google_kms_crypto_key_iam_member.pd_compute,
    google_project_iam_member.nodes,
  ]
}
