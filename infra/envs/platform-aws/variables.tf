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

variable "bootstrap_public_cidrs" {
  description = "첫 설치 동안만 EKS API를 열 운영자 IP(/32). 설치가 끝나면 비움(modules/cluster/eks 참고)"
  type        = list(string)
  default     = []
}

variable "admin_principal_arns" {
  description = "클러스터 관리자 access entry를 줄 IAM 주체(선택). 생성자는 따로 자동으로 받음"
  type        = list(string)
  default     = []
}

variable "github_repository" {
  description = "CI OIDC가 신뢰할 GitHub 저장소(owner/repo)"
  type        = string
  default     = "mangowhoiscloud/b-free"
}

variable "github_ref" {
  description = "CI OIDC가 신뢰할 ref 하나"
  type        = string
  default     = "refs/heads/main"
}

variable "platform_images" {
  description = "플랫폼 ECR 저장소를 만들 이미지 이름. 저장소 이름은 <name>/<이미지>"
  type        = list(string)
  default     = ["api", "console", "gate", "collector", "executor", "rehearsal", "link"]
}

variable "source_cluster_name" {
  description = <<-EOT
    플랫폼이 수집 · 지휘할 출발지 EKS 이름(같은 계정, 같은 리전). null이면 출발지 링크 자원을 만들지 않음.
    access entry는 출발지 클러스터에 만들지만 주인은 플랫폼 환경임(D-21 "플랫폼에서 출발지 EKS로 가는 access entry").
  EOT
  type        = string
  default     = "bravo-source"
}
