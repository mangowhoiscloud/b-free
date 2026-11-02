# 고객 관리 키 둘: K8s Secret 애플리케이션 계층 암호화용, PD(노드 부팅 디스크와 PV)용.
# Cloud KMS 키링과 키는 지울 수 없음. 다시 올릴 때 이름이 겹치지 않게 접미사를 붙임.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  gke_service_agent     = "serviceAccount:service-${data.google_project.this.number}@container-engine-robot.iam.gserviceaccount.com"
  compute_service_agent = "serviceAccount:service-${data.google_project.this.number}@compute-system.iam.gserviceaccount.com"
}

resource "random_id" "kms" {
  byte_length = 3
}

resource "google_kms_key_ring" "this" {
  project  = var.project_id
  name     = "${var.name}-${random_id.kms.hex}"
  location = var.region
}

resource "google_kms_crypto_key" "secrets" {
  name            = "gke-secrets"
  key_ring        = google_kms_key_ring.this.id
  rotation_period = "7776000s"
  labels          = var.labels
}

resource "google_kms_crypto_key" "pd" {
  name            = "pd"
  key_ring        = google_kms_key_ring.this.id
  rotation_period = "7776000s"
  labels          = var.labels
}

resource "google_kms_crypto_key_iam_member" "secrets_gke" {
  crypto_key_id = google_kms_crypto_key.secrets.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = local.gke_service_agent
}

# PD CSI가 만든 디스크와 노드 부팅 디스크는 Compute Engine 서비스 에이전트가 키를 씀(GKE CMEK 문서).
resource "google_kms_crypto_key_iam_member" "pd_compute" {
  crypto_key_id = google_kms_crypto_key.pd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = local.compute_service_agent
}
