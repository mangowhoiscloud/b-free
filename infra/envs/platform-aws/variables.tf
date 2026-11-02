# 고객이 넣는 값은 이름, 리전, 크기뿐입니다. CNI, CSI, 노드 설정은 아래 프리셋과 모듈이 정합니다.

variable "name" {
  description = "클러스터와 VPC 이름"
  type        = string
  default     = "bravo-platform"
}

variable "region" {
  description = "AWS 리전"
  type        = string
  default     = "ap-northeast-2"
}

variable "size" {
  description = "크기 프리셋. small(데모) 또는 medium"
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium"], var.size)
    error_message = "size는 small 또는 medium입니다."
  }
}

variable "gitops_repo_url" {
  description = "Argo CD가 당겨 갈 배포 저장소 URL(제안 D-16, 06-tech-stack.md 6절)"
  type        = string
}

variable "tags" {
  description = "추가 태그(선택)"
  type        = map(string)
  default     = {}
}
