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
  description = "KMS 키링 리전. 클러스터 위치(location)의 리전과 같아야 함"
  type        = string
}

variable "location" {
  description = "클러스터 위치. 존(예: asia-northeast1-b)이면 zonal 제어면, 리전이면 regional 제어면"
  type        = string
}

variable "kubernetes_version" {
  description = "최소 K8s minor. 모든 환경 공통 1.36(bravo-source EKS 1.36, 온프렘 K3s 1.36)"
  type        = string
  default     = "1.36"
}

variable "release_channel" {
  description = "GKE 릴리스 채널. 2026-11-02 asia-northeast1 REGULAR 기본판 1.36.4-gke.2046003(get-server-config로 확인)"
  type        = string
  default     = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.release_channel)
    error_message = "RAPID, REGULAR, STABLE 중 하나입니다."
  }
}

variable "node_pools" {
  description = "노드 풀. 키는 풀 이름. C4A(Arm)는 Hyperdisk만 붙으므로 disk_type은 hyperdisk-balanced"
  type = map(object({
    machine_type = string
    zones        = list(string)
    min_per_zone = number
    max_per_zone = number
    disk_type    = optional(string, "hyperdisk-balanced")
    disk_size_gb = optional(number, 50)
    # Arm 노드 taint: NONE(모두 Arm인 클러스터) | ARM(x86 풀과 섞을 때)
    architecture_taint_behavior = optional(string, "NONE")
    labels                      = optional(map(string), {})
    taints = optional(list(object({
      key    = string
      value  = string
      effect = string
    })), [])
  }))
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

variable "enable_hubble_relay" {
  description = "Dataplane V2 관측(관리형 Hubble relay)"
  type        = bool
  default     = true
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
