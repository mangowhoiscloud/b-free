variable "name" {
  description = "역할 이름 접두어. 예: bravo-platform-ci"
  type        = string
}

variable "github_repository" {
  description = "신뢰할 GitHub 저장소(owner/repo). 예: mangowhoiscloud/b-free"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "owner/repo 형식이어야 합니다."
  }
}

variable "github_sub_prefix" {
  description = "sub 클레임 접두어. null이면 repo:<owner>/<repo>. 변하지 않는 subject를 쓰는 저장소는 sub_claim_prefix 값"
  type        = string
  default     = null

  validation {
    condition     = var.github_sub_prefix == null || can(regex("^repo:[^*?:]+$", var.github_sub_prefix))
    error_message = "repo: 로 시작하고 와일드카드와 콜론이 없는 접두어 하나만 받습니다."
  }
}

variable "github_ref" {
  description = "신뢰할 Git ref 하나. 이 ref의 워크플로만 역할을 받음(NFR-6). 예: refs/heads/main"
  type        = string
  default     = "refs/heads/main"

  validation {
    condition     = can(regex("^refs/(heads|tags)/[^*?]+$", var.github_ref))
    error_message = "와일드카드 없는 refs/heads/<branch> 또는 refs/tags/<tag> 하나만 받습니다."
  }
}

variable "create_oidc_provider" {
  description = <<-EOT
    true면 GitHub OIDC 공급자(token.actions.githubusercontent.com)를 만듦.
    false면 계정에 이미 있는 공급자를 data로 찾아 씀(계정당 URL 하나만 둘 수 있음).
  EOT
  type        = bool
  default     = false
}

variable "ecr_repository_arns" {
  description = "이미지를 push할 ECR 저장소 ARN. 이 저장소에만 쓰기 권한을 줌"
  type        = list(string)

  validation {
    condition     = length(var.ecr_repository_arns) > 0
    error_message = "push할 저장소가 하나 이상 필요합니다."
  }
}

variable "max_session_duration" {
  description = "역할 세션 최대 길이(초). 빌드 한 번에 맞춘 1시간"
  type        = number
  default     = 3600
}

variable "tags" {
  description = "추가 태그"
  type        = map(string)
  default     = {}
}
