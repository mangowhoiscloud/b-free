# 고객이 넣는 값은 프로젝트, 이름, 리전, 크기뿐입니다. CNI, CSI, 노드는 Autopilot과 모듈이 정합니다.

variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "name" {
  description = "클러스터와 VPC 이름"
  type        = string
  default     = "bravo-target-gcp"
}

variable "region" {
  description = "GCP 리전. 서울은 asia-northeast3"
  type        = string
  default     = "asia-northeast3"
}

variable "size" {
  description = "크기 프리셋. Autopilot은 노드를 Google이 고르므로 저장 용량 상한만 바뀜"
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium"], var.size)
    error_message = "size는 small 또는 medium입니다."
  }
}

variable "deletion_protection" {
  description = "클러스터 삭제 보호. 지우기 전에 false로 apply"
  type        = bool
  default     = true
}

variable "gitops_repo_url" {
  description = "Argo CD가 당겨 갈 배포 저장소 URL"
  type        = string
}

variable "labels" {
  description = "추가 라벨(선택)"
  type        = map(string)
  default     = {}
}
