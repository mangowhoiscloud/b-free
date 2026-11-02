# CNPG chat-db 백업 저장소(DR-01, 게이트 DR-RESTORE · DR-ARCHIVE). 첨부 버킷과 다른 버킷.
# - 버킷: WAL과 base backup(barman-cloud, deploy/landing-zone/overlays/gke/manifests/32-chat-db-backup.yaml)을 받음.
#   공개 접근 방지(enforced), 균일 버킷 접근, CMEK(전용 키 db-backup).
# - 권한: Workload Identity Federation for GKE의 주체(principal://)에 버킷 역할을 바로 줌. GSA도 키도 없음(D-26).
#   KSA chat/chat-db(CNPG가 만듦)는 읽기 · 쓰기, chat/chat-db-restore(복구 검증 클러스터)는 읽기만.
#   barman-cloud는 시작할 때 버킷이 있는지 GET으로 확인함(storage.buckets.get). 그래서 legacyBucketReader를 함께 줌.

locals {
  wi_principal_prefix       = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject"
  chat_db_principal         = "${local.wi_principal_prefix}/ns/chat/sa/chat-db"
  chat_db_restore_principal = "${local.wi_principal_prefix}/ns/chat/sa/chat-db-restore"
}

resource "google_kms_crypto_key" "db_backup" {
  name            = "db-backup"
  key_ring        = google_kms_key_ring.storage.id
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key_iam_member" "db_backup_gcs" {
  crypto_key_id = google_kms_crypto_key.db_backup.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_storage_bucket" "db_backup" {
  project  = var.project_id
  name     = "${var.name}-db-backup-${random_id.bucket.hex}"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # 데모 환경. 운영은 false

  encryption {
    default_kms_key_name = google_kms_crypto_key.db_backup.id
  }

  # 데모 비용: 지운 객체를 7일 보관하지 않음. 보존은 CNPG retentionPolicy가 정함.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  depends_on = [google_kms_crypto_key_iam_member.db_backup_gcs]
}

resource "google_storage_bucket_iam_member" "chat_db_objects" {
  bucket = google_storage_bucket.db_backup.name
  role   = "roles/storage.objectUser"
  member = local.chat_db_principal
}

resource "google_storage_bucket_iam_member" "chat_db_bucket" {
  bucket = google_storage_bucket.db_backup.name
  role   = "roles/storage.legacyBucketReader"
  member = local.chat_db_principal
}

resource "google_storage_bucket_iam_member" "chat_db_restore_objects" {
  bucket = google_storage_bucket.db_backup.name
  role   = "roles/storage.objectViewer"
  member = local.chat_db_restore_principal
}

resource "google_storage_bucket_iam_member" "chat_db_restore_bucket" {
  bucket = google_storage_bucket.db_backup.name
  role   = "roles/storage.legacyBucketReader"
  member = local.chat_db_restore_principal
}

output "db_backup_bucket" {
  description = "CNPG chat-db 백업 버킷(barman-cloud). 첨부 버킷과 다름"
  value       = google_storage_bucket.db_backup.name
}
