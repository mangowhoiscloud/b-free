variable "name" {
  description = "클러스터 이름. 예: bravo-target-gcp"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,38}$", var.name))
    error_message = "소문자, 숫자, 하이픈으로 3–39자여야 합니다."
  }
}

variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "region" {
  description = "리전 클러스터 위치. 예: asia-northeast3"
  type        = string
}

variable "kubernetes_version" {
  description = "최소 K8s minor. release_channel 안에서 이 판 이상을 씀. 모든 환경 공통 1.36"
  type        = string
  default     = "1.36"
}

variable "release_channel" {
  description = "GKE 릴리스 채널. 1.36은 2026-11-02 현재 REGULAR에 있고 STABLE에는 2026-11-11 무렵 들어옴(GKE 출시 일정)"
  type        = string
  default     = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.release_channel)
    error_message = "RAPID, REGULAR, STABLE 중 하나입니다."
  }
}

variable "network_self_link" {
  description = "network/gcp의 network_self_link"
  type        = string
}

variable "subnet_self_link" {
  description = "network/gcp의 subnet_self_link"
  type        = string
}

variable "pod_range_name" {
  description = "network/gcp의 pod_range_name"
  type        = string
}

variable "service_range_name" {
  description = "network/gcp의 service_range_name"
  type        = string
}

variable "deletion_protection" {
  description = "클러스터 삭제 보호. 지우려면 먼저 false로 apply"
  type        = bool
  default     = true
}

variable "kubeconfig_path" {
  description = "kubeconfig를 쓸 경로. 비밀이 없음(gke-gcloud-auth-plugin exec 방식)"
  type        = string
}

variable "labels" {
  description = "GCP 자원 라벨(component 등)"
  type        = map(string)
  default     = {}
}
