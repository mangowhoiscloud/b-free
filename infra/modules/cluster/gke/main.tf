# GKE Standard 클러스터(D-21, TECH-STACK-SPEC 1절). Dataplane V2는 생성 때만 정할 수 있어 여기서 고정.
# Autopilot은 kube-bench용 특권 Pod를 막아 쓰지 않음(TECH-STACK-SPEC 2.1).
# 노드 풀은 이 모듈 안에 둠(infra/README.md: node 모듈 없음). 고객 입력은 크기뿐이고 머신 종류는 env가 고름.

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
  location = var.location

  deletion_protection = var.deletion_protection

  # 기본 노드 풀을 만들지 않음(1.36.4-gke.1469000+: initial_node_count = 0). 아래 google_container_node_pool만 씀.
  initial_node_count = 0

  network    = var.network_self_link
  subnetwork = var.subnet_self_link

  # Dataplane V2(eBPF, Cilium 기반). NetworkPolicy 집행과 흐름 기록(관리형 Hubble)이 여기에 기댐.
  datapath_provider = "ADVANCED_DATAPATH"

  min_master_version = var.kubernetes_version

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pod_range_name
    services_secondary_range_name = var.service_range_name
  }

  # 노드는 공인 IP가 없음. API는 VPC 안 사설 IP와 IAM으로 지키는 DNS 엔드포인트로만 받음.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = true
  }

  # 사설 엔드포인트에는 허가 대역이 필요함. 대역을 비워 VPC 안만 받음. 운영자 kubectl은 아래 DNS 엔드포인트(IAM)로.
  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled = false
  }

  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
  }

  # 환경 안 진입점(TECH-STACK-SPEC 1절). Dataplane V2에서는 Cilium Gateway를 켤 수 없어 GKE Gateway 컨트롤러를 씀.
  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  database_encryption {
    state    = "ENCRYPTED"
    key_name = google_kms_crypto_key.secrets.id
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # 감사 로그(AUD-01): 관리 활동 감사 로그는 GKE가 늘 Cloud Logging에 씀(무료). 워크로드 로그는 비용 때문에 끔.
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = false
    }
    # 관리형 Hubble(Dataplane V2 관측). 흐름 기록이 NET-01의 증거(TECH-STACK-SPEC 1절).
    advanced_datapath_observability_config {
      # Dataplane V2 flow metrics on anetd :9965 (scraped by the landing-zone otel-agent, 15-r2-contracts 7).
      # Turned on for bravo-target-gcp on 2026-11-02 with gcloud --enable-dataplane-v2-metrics; this keeps it on.
      enable_metrics = true
      enable_relay   = var.enable_hubble_relay
    }
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

resource "google_container_node_pool" "this" {
  for_each = var.node_pools

  project  = var.project_id
  name     = each.key
  location = var.location
  cluster  = google_container_cluster.this.name

  node_locations = each.value.zones

  autoscaling {
    min_node_count = each.value.min_per_zone
    max_node_count = each.value.max_per_zone
  }
  initial_node_count = each.value.min_per_zone

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type    = each.value.machine_type
    disk_type       = each.value.disk_type
    disk_size_gb    = each.value.disk_size_gb
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    boot_disk_kms_key = google_kms_crypto_key.pd.id

    # 메타데이터 차단(11-landing-zone.md 3절): Pod는 노드 SA가 아니라 Workload Identity로만 GCP를 부름.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_integrity_monitoring = true
    }

    # GKE는 Arm 노드에 kubernetes.io/arch=arm64:NoSchedule을 붙임. 노드가 모두 Arm이면 모든 차트에 toleration을
    # 넣어야 하므로 끔(NONE). x86 풀을 섞을 때만 ARM으로 둠.
    taint_config {
      architecture_taint_behavior = each.value.architecture_taint_behavior
    }

    labels = merge({ "bravo.io/pool" = each.key }, each.value.labels)

    dynamic "taint" {
      for_each = each.value.taints
      content {
        key    = taint.value.key
        value  = taint.value.value
        effect = taint.value.effect
      }
    }

    resource_labels = var.labels
  }

  lifecycle {
    # 오토스케일러가 바꾸는 노드 수는 Terraform이 되돌리지 않음.
    ignore_changes = [initial_node_count]
  }
}
