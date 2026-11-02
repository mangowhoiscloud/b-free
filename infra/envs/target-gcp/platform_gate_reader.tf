# 플랫폼 게이트(platform/gate, FR-6 · FR-12)의 GKE 읽기 신원. 수집기(platform_cost_reader.tf)와 다른 GCP 서비스 계정
# bravo-gate-ro를 같은 WIF 풀(bravo-platform, 공급자 rke2)로 가장함. 장기 키 없음(D-26).
# 주체: 플랫폼 RKE2 서비스 계정 bravo-control/gate. IAM은 container.clusterViewer(클러스터 조회 · DNS 엔드포인트
# 접속 container.clusters.connect)뿐이고, 클러스터 안 권한은 K8s RBAC(deploy/platform/link/gke-gate/gate.yaml)이
# 정함: SubjectAccessReview 만들기, ns chat 의 NetworkPolicy · CNPG Cluster 읽기, 네임스페이스 읽기.

locals {
  gate_reader_subject = "system:serviceaccount:bravo-control:gate"
  gate_reader_member  = local.cost_reader_enabled ? "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.platform[0].workload_identity_pool_id}/subject/${local.gate_reader_subject}" : null
}

resource "google_service_account" "gate_reader" {
  count        = local.cost_reader_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "bravo-gate-ro"
  display_name = "Bravo platform gate (read-only control probes)"
}

resource "google_service_account_iam_member" "gate_reader_wif" {
  count              = local.cost_reader_enabled ? 1 : 0
  service_account_id = google_service_account.gate_reader[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.gate_reader_member
}

# 가장 요청의 할당량 프로젝트(수집기와 같은 이유, platform_cost_reader.tf).
resource "google_project_iam_member" "gate_reader_principal_quota" {
  count   = local.cost_reader_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = local.gate_reader_member
}

resource "google_project_iam_member" "gate_reader_cluster_viewer" {
  count   = local.cost_reader_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/container.clusterViewer"
  member  = "serviceAccount:${google_service_account.gate_reader[0].email}"
}

output "platform_gate_reader" {
  description = "플랫폼 게이트 WIF 자격 설정에 넣을 값(비밀 아님)"
  value = local.cost_reader_enabled ? {
    audience       = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.platform_rke2[0].name}"
    token_audience = local.cost_reader_audience
    subject        = local.gate_reader_subject
    impersonate    = google_service_account.gate_reader[0].email
  } : null
}

# OBJ-ENC on GCS: the XML API has no GetBucketEncryption (NotImplemented), so the gate reads the bucket's default
# Cloud KMS key with the JSON API buckets.get. storage.bucketViewer = storage.buckets.get/list only, no object access.
resource "google_storage_bucket_iam_member" "gate_reader_attachments_metadata" {
  count  = local.cost_reader_enabled ? 1 : 0
  bucket = google_storage_bucket.attachments.name
  role   = "roles/storage.bucketViewer"
  member = "serviceAccount:${google_service_account.gate_reader[0].email}"
}
