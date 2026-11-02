# 고객이 넣는 값은 이름, 리전, 크기뿐입니다. CNI, CSI, 노드 설정은 아래 프리셋과 모듈이 정합니다.

variable "name" {
  description = "클러스터, VPC, 버킷 접두어 이름. 소문자로 시작, 3–24자(S3 버킷 접두어 길이 한도)"
  type        = string
  default     = "bravo-source"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name))
    error_message = "소문자, 숫자, 하이픈으로 3–24자여야 합니다."
  }
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

variable "demo_weak_bucket_policy" {
  description = <<-EOT
    S1 데모용 약한 첨부 버킷 정책. 기본 false(강한 정책: TLS 강제, 객체 동작은 앱 역할만).
    true면 TLS 강제가 없고 계정 전체에 s3:*를 엶. 공개 정책은 아니라서 공개 접근 차단은 그대로임.
  EOT
  type        = bool
  default     = false
}

variable "bucket_admin_principal_arns" {
  description = "강한 정책에서도 첨부 객체를 다룰 수 있는 관리자 IAM 주체(선택). 비우면 앱 역할만"
  type        = list(string)
  default     = []
}

variable "bootstrap_public_cidrs" {
  description = "첫 설치 동안만 EKS API를 열 운영자 IP(/32). 설치가 끝나면 비움(modules/cluster/eks 참고)"
  type        = list(string)
  default     = []
}

variable "admin_principal_arns" {
  description = "클러스터 관리자 access entry를 줄 IAM 주체(선택)"
  type        = list(string)
  default     = []
}
