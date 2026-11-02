# 고객이 넣는 값은 프로젝트, 이름, 지역(존), 크기뿐입니다. CNI, CSI, 노드 종류는 모듈과 프리셋이 정합니다.

variable "project_id" {
  description = "GCP 프로젝트 ID(전용 프로젝트). gcloud 기본 설정에 기대지 않고 늘 명시"
  type        = string
}

variable "name" {
  description = "클러스터와 VPC 이름"
  type        = string
  default     = "bravo-target-gcp"
}

variable "region" {
  description = "GCP 리전. Arm(C4A) 노드가 있는 도쿄 asia-northeast1(서울 asia-northeast3에는 C4A 없음)"
  type        = string
  default     = "asia-northeast1"
}

variable "zone" {
  description = "클러스터 존(zonal 제어면, 노드 풀). C4A가 있는 존"
  type        = string
  default     = "asia-northeast1-b"
}

variable "size" {
  description = "크기 프리셋(small, medium). 노드 수 범위만 바뀜"
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium"], var.size)
    error_message = "size는 small 또는 medium입니다."
  }
}

variable "deletion_protection" {
  description = "클러스터 삭제 보호. 데모 환경은 scripts/down.sh 한 번으로 지우도록 terraform.tfvars에서 false"
  type        = bool
  default     = true
}

variable "enforce_storage_https" {
  description = "조직 정책 storage.secureHttpTransport(평문 HTTP 거부, OBJ-06). 조직 아래 프로젝트에서만 됨(조직 없는 프로젝트는 403)"
  type        = bool
  default     = false
}

variable "labels" {
  description = "추가 라벨(선택)"
  type        = map(string)
  default     = {}
}
