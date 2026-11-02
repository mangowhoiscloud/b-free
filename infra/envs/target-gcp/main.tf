# GCP 목적지(D-01, D-21, D-22, D-23). GKE Standard + Dataplane V2, 같은 랜딩 존(deploy/landing-zone/overlays/gke).
# 여기서 만드는 것: VPC(사설 노드, Cloud NAT, 프록시 전용 서브넷), GKE, 이미지 저장소, 첨부 버킷(GCS),
# Gateway 고정 내부 주소, 앱과 게이트 시험용 서비스 계정. 비밀 값은 만들지 않음(D-26): HMAC 키, DB 비밀번호,
# 터널 토큰은 scripts/secrets.sh가 플랫폼 OpenBao에 바로 씀.

locals {
  # C4A(Axion, Arm)는 2026-11-02 기준 asia-northeast1-b · c에만 있음(asia-northeast3에는 없음, machine-types list로 확인).
  # 데모 이미지가 arm64 단일 판이라 출발지(EKS t4g)와 같은 digest를 그대로 띄우려면 Arm 노드가 필요함.
  presets = {
    small  = { machine_type = "c4a-standard-4", min = 1, max = 2, storage_quota = "50Gi" }
    medium = { machine_type = "c4a-standard-4", min = 2, max = 4, storage_quota = "200Gi" }
  }
  preset = local.presets[var.size]

  labels = merge(var.labels, {
    component  = "target"
    env        = "target-gcp"
    managed-by = "terraform"
  })

  kubeconfig_path = "${abspath(path.root)}/.terraform/bravo/kubeconfig"

  # 앱 컨테이너 포트(apps/demo/chart). GKE Gateway는 NEG로 Pod에 바로 보내므로 이 포트만 열면 됨.
  app_port = 8000
  # Google 부하 분산기 상태 확인 대역(공식 문서 고정 대역).
  health_check_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
}

resource "google_project_service" "this" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "cloudkms.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "network" {
  source = "../../modules/network/gcp"

  name            = var.name
  project_id      = var.project_id
  region          = var.region
  proxy_only_cidr = "10.30.4.0/23"

  depends_on = [google_project_service.this]
}

module "cluster" {
  source = "../../modules/cluster/gke"

  name                = var.name
  project_id          = var.project_id
  region              = var.region
  location            = var.zone
  kubernetes_version  = "1.36"
  release_channel     = "REGULAR"
  network_self_link   = module.network.network_self_link
  subnet_self_link    = module.network.subnet_self_link
  pod_range_name      = module.network.pod_range_name
  service_range_name  = module.network.service_range_name
  deletion_protection = var.deletion_protection
  kubeconfig_path     = local.kubeconfig_path
  labels              = local.labels

  node_pools = {
    core = {
      machine_type = local.preset.machine_type
      zones        = [var.zone]
      min_per_zone = local.preset.min
      max_per_zone = local.preset.max
      disk_type    = "hyperdisk-balanced"
      disk_size_gb = 50
    }
  }

  depends_on = [google_project_service.this]
}

# GKE Gateway(gke-l7-rilb)의 프록시와 상태 확인이 Pod에 닿게 함. 노드 서비스 계정이 붙은 VM만 대상.
resource "google_compute_firewall" "gateway_to_pods" {
  project   = var.project_id
  name      = "${var.name}-gateway-to-pods"
  network   = module.network.network_self_link
  direction = "INGRESS"
  priority  = 1000

  source_ranges           = concat([module.network.proxy_only_cidr], local.health_check_ranges)
  target_service_accounts = [module.cluster.node_service_account]

  allow {
    protocol = "tcp"
    ports    = [tostring(local.app_port)]
  }
}

# Gateway bravo-gateway의 고정 내부 주소. cf-edge(cloudflared)는 이 주소의 80으로 보냄.
resource "google_compute_address" "gateway" {
  project      = var.project_id
  name         = "${var.name}-gateway"
  region       = var.region
  address_type = "INTERNAL"
  purpose      = "SHARED_LOADBALANCER_VIP"
  subnetwork   = module.network.subnet_self_link
}

# ---------------------------------------------------------------- 이미지 저장소(목적지 레지스트리, 계약 5절)

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "bravo"
  format        = "DOCKER"
  description   = "Bravo 목적지 이미지. 출발지와 같은 digest로만 배포"

  docker_config {
    immutable_tags = true
  }

  depends_on = [google_project_service.this]
}

resource "google_artifact_registry_repository_iam_member" "nodes_read" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${module.cluster.node_service_account}"
}

# ---------------------------------------------------------------- 첨부 객체 저장(D-22: 같은 S3 계약, GKE 엔진은 GCS)

resource "random_id" "bucket" {
  byte_length = 3
}

data "google_storage_project_service_account" "gcs" {
  project = var.project_id

  depends_on = [google_project_service.this]
}

# 버킷 기본 암호화 키(ENC-02). Cloud KMS 키링은 지울 수 없어 이름에 접미사를 붙임.
resource "google_kms_key_ring" "storage" {
  project  = var.project_id
  name     = "${var.name}-storage-${random_id.bucket.hex}"
  location = var.region

  depends_on = [google_project_service.this]
}

resource "google_kms_crypto_key" "attachments" {
  name            = "attachments"
  key_ring        = google_kms_key_ring.storage.id
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key_iam_member" "attachments_gcs" {
  crypto_key_id = google_kms_crypto_key.attachments.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
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

  encryption {
    default_kms_key_name = google_kms_crypto_key.attachments.id
  }

  # 데모 비용: 지운 객체를 7일 보관하지 않음
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_kms_crypto_key_iam_member.attachments_gcs]
}

# 게이트 OBJ 시험의 고정 키 시험 객체(404로 거짓 통과하지 않게, platform/gate probes/objstore.py). 비밀 아님.
resource "google_storage_bucket_object" "canary" {
  bucket       = google_storage_bucket.attachments.name
  name         = "_gate/canary"
  content      = "bravo gate canary (target-gcp)\n"
  content_type = "text/plain"
}

# 앱 자격(S3 계약의 HMAC 키 주인). 버킷 하나에서 객체 읽기 · 쓰기만. HMAC 키는 scripts/secrets.sh가 만들고
# 값은 OpenBao secret/envs/target-gcp/chat-s3에만 씀(D-26). Terraform 상태에 비밀 없음.
resource "google_service_account" "chat_app" {
  project      = var.project_id
  account_id   = "chat-app"
  display_name = "chat app attachments (HMAC, S3 contract)"
}

resource "google_storage_bucket_iam_member" "chat_app" {
  bucket = google_storage_bucket.attachments.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.chat_app.email}"
}

# OBJ-05 대조용 다른 자격. 이 버킷에 아무 권한이 없음.
resource "google_service_account" "gate_other" {
  project      = var.project_id
  account_id   = "gate-other"
  display_name = "gate OBJ-05 other credential (no bucket access)"
}

# OBJ-06(평문 HTTP 거부). GCS XML API는 기본으로 http://를 받음. 막는 수단은 조직 정책
# storage.secureHttpTransport인데, 조직(Organization) 아래 프로젝트에서만 걸 수 있음. 이 데모 프로젝트는
# 조직이 없어 2026-11-02 apply에서 403(orgpolicy.policies.create)이 났음. 그래서 기본값 false이고,
# 클러스터 쪽 보완 통제로 아래 egress 방화벽(노드에서 바깥 80 차단)을 둠. OBJ-06 결과는 그대로 보고함.
resource "google_project_service" "orgpolicy" {
  count = var.enforce_storage_https ? 1 : 0

  project            = var.project_id
  service            = "orgpolicy.googleapis.com"
  disable_on_destroy = false
}

resource "google_org_policy_policy" "storage_https" {
  count = var.enforce_storage_https ? 1 : 0

  name   = "projects/${var.project_id}/policies/storage.secureHttpTransport"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.orgpolicy]
}

# 보완 통제: 클러스터에서 바깥으로 나가는 평문 HTTP(80)를 막음. VPC 안(Gateway 내부 주소 등)은 허용.
resource "google_compute_firewall" "egress_allow_internal_http" {
  project            = var.project_id
  name               = "${var.name}-egress-allow-internal-80"
  network            = module.network.network_self_link
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["10.0.0.0/8"]

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
}

resource "google_compute_firewall" "egress_deny_http" {
  project            = var.project_id
  name               = "${var.name}-egress-deny-80"
  network            = module.network.network_self_link
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "tcp"
    ports    = ["80"]
  }
}
