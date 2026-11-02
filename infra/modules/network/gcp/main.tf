# 노드 서브넷 하나와 Pod, Service 보조 대역. 노드는 공인 IP가 없고 Cloud NAT로만 나감(제안 D-16과 같은 원칙).

locals {
  pod_range_name     = "${var.name}-pods"
  service_range_name = "${var.name}-services"
}

resource "google_compute_network" "this" {
  project                         = var.project_id
  name                            = var.name
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "nodes" {
  project                  = var.project_id
  name                     = "${var.name}-nodes"
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.node_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = local.pod_range_name
    ip_cidr_range = var.pod_cidr
  }

  secondary_ip_range {
    range_name    = local.service_range_name
    ip_cidr_range = var.service_cidr
  }

  dynamic "log_config" {
    for_each = var.enable_flow_logs ? [1] : []
    content {
      aggregation_interval = "INTERVAL_5_SEC"
      flow_sampling        = 0.5
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_router" "this" {
  project = var.project_id
  name    = "${var.name}-router"
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  project                            = var.project_id
  name                               = "${var.name}-nat"
  router                             = google_compute_router.this.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.nodes.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
