# 두 번째 목적지(제안 D-01). GKE Autopilot 리전 클러스터. validate만 함.

locals {
  presets = {
    small  = { storage_quota = "50Gi" }
    medium = { storage_quota = "200Gi" }
  }
  preset = local.presets[var.size]

  kubeconfig_path = "${abspath(path.root)}/.terraform/bravo/kubeconfig"
  gitops_dir      = "${abspath(path.root)}/gitops"
}

resource "google_project_service" "this" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "cloudkms.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "network" {
  source = "../../modules/network/gcp"

  name       = var.name
  project_id = var.project_id
  region     = var.region

  depends_on = [google_project_service.this]
}

module "cluster" {
  source = "../../modules/cluster/gke"

  name                = var.name
  project_id          = var.project_id
  region              = var.region
  kubernetes_version  = "1.36"
  network_self_link   = module.network.network_self_link
  subnet_self_link    = module.network.subnet_self_link
  pod_range_name      = module.network.pod_range_name
  service_range_name  = module.network.service_range_name
  deletion_protection = var.deletion_protection
  kubeconfig_path     = local.kubeconfig_path
}

# 첨부 객체 저장. 같은 S3 계약, 환경마다 다른 엔진(GKE는 Cloud Storage). 공개 접근은 막음.
resource "random_id" "bucket" {
  byte_length = 3
}

resource "google_storage_bucket" "attachments" {
  project  = var.project_id
  name     = "${var.name}-attachments-${random_id.bucket.hex}"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # 데모 환경. 운영은 false

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.this]
}

# 고객 클러스터와 같은 landing zone 묶음(deploy/landing-zone). gke overlay는 PD 기반 StorageClass와
# GCS 객체 저장 계약만 다름. Argo CD Application 파일만 쓰고 클러스터에 적용하지 않음.
module "landing_zone" {
  source = "../../modules/landing_zone"

  provider_kind = module.cluster.storage_platform
  delivery      = module.cluster.addon_delivery
  argocd = {
    repo_url        = var.gitops_repo_url
    target_revision = "main"
    path            = "infra/envs/target-gcp/gitops"
  }
  output_dir = local.gitops_dir
  vars = {
    cluster_name        = module.cluster.name
    project_id          = var.project_id
    region              = var.region
    kms_key_arn         = module.cluster.pd_kms_key_id
    storage_quota       = local.preset.storage_quota
    object_store_bucket = google_storage_bucket.attachments.name
    object_store_region = var.region
  }
}
