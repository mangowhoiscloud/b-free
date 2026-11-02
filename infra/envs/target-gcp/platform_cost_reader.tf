# 플랫폼 수집기의 GCP 읽기(T1 비용 영수증, NFR-8, platform/collector GET /v1/cost). 장기 키 없음(NFR-6, D-26):
# 플랫폼 RKE2의 서비스 계정 토큰(발급자 = S3 공개 OIDC 문서, infra/envs/platform-aws rke2.tf)을 Workload Identity
# Federation으로 받음. 주체는 bravo-control/collector 서비스 계정 하나이고, GCP 서비스 계정 bravo-cost-reader를
# 가장(impersonation)함. Cloud Billing Catalog API가 연합 토큰을 바로 받지 않기 때문(2026-11-02 실측 401). 역할은 읽기뿐.
# 자원 목록 · 크기 · 생성 시각만 읽음. 비용은 Cloud Billing Catalog API(공개 가격표)로 계산하고, 청구 데이터는 읽지 않음.

variable "platform_oidc_issuer" {
  description = "플랫폼 RKE2 서비스 계정 토큰 발급자(S3 공개 OIDC discovery). null이면 아무것도 만들지 않음"
  type        = string
  default     = "https://bravo-platform-oidc-721622471953.s3.ap-northeast-2.amazonaws.com"
}

locals {
  cost_reader_enabled  = var.platform_oidc_issuer != null
  cost_reader_subject  = "system:serviceaccount:bravo-control:collector"
  cost_reader_audience = "bravo-platform-gcp-wif"
  cost_reader_roles = toset([
    "roles/compute.viewer",                    # 인스턴스 · 디스크 · Cloud NAT · 주소 · 전달 규칙
    "roles/container.clusterViewer",           # GKE 클러스터 목록
    "roles/artifactregistry.reader",           # 저장소 크기
    "roles/cloudkms.viewer",                   # 키 버전(값은 없음)
    "roles/serviceusage.serviceUsageConsumer", # Cloud Billing Catalog API 호출의 할당량 프로젝트
  ])
}

resource "google_project_service" "sts" {
  count              = local.cost_reader_enabled ? 1 : 0
  project            = var.project_id
  service            = "sts.googleapis.com"
  disable_on_destroy = false
}

resource "google_iam_workload_identity_pool" "platform" {
  count                     = local.cost_reader_enabled ? 1 : 0
  project                   = var.project_id
  workload_identity_pool_id = "bravo-platform"
  display_name              = "Bravo platform RKE2"
  description               = "Platform service accounts (read-only cost collector)"
}

resource "google_iam_workload_identity_pool_provider" "platform_rke2" {
  count                              = local.cost_reader_enabled ? 1 : 0
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.platform[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "rke2"
  display_name                       = "platform RKE2 issuer"
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition                = "assertion.sub == '${local.cost_reader_subject}'"
  oidc {
    issuer_uri        = var.platform_oidc_issuer
    allowed_audiences = [local.cost_reader_audience]
  }
  depends_on = [google_project_service.sts]
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_service_account" "cost_reader" {
  count        = local.cost_reader_enabled ? 1 : 0
  project      = var.project_id
  account_id   = "bravo-cost-reader"
  display_name = "Bravo platform cost collector (read-only)"
}

resource "google_service_account_iam_member" "cost_reader_wif" {
  count              = local.cost_reader_enabled ? 1 : 0
  service_account_id = google_service_account.cost_reader[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.platform[0].workload_identity_pool_id}/subject/${local.cost_reader_subject}"
}

# 가장 요청(iamcredentials)도 할당량 프로젝트(x-goog-user-project)를 쓰므로 연합 주체 자신에게도 필요(2026-11-02 실측
# USER_PROJECT_DENIED). 이 역할은 API 사용 권한뿐이고 자원 읽기 권한은 없음.
resource "google_project_iam_member" "cost_reader_principal_quota" {
  count   = local.cost_reader_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.platform[0].workload_identity_pool_id}/subject/${local.cost_reader_subject}"
}

resource "google_project_iam_member" "cost_reader" {
  for_each = local.cost_reader_enabled ? local.cost_reader_roles : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.cost_reader[0].email}"
}

output "platform_cost_reader" {
  description = "플랫폼 수집기 WIF 자격 설정에 넣을 값(비밀 아님)"
  value = local.cost_reader_enabled ? {
    audience       = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.platform_rke2[0].name}"
    token_audience = local.cost_reader_audience
    subject        = local.cost_reader_subject
    impersonate    = google_service_account.cost_reader[0].email
  } : null
}
