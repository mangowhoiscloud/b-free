variable "name" {
  description = "VPC 이름 접두어. 예: bravo-platform"
  type        = string
}

variable "cidr" {
  description = "VPC 대역. 01-environments.md 4절의 표를 따름"
  type        = string
}

variable "azs" {
  description = "사설 서브넷을 둘 가용 영역 목록. private_subnet_cidrs와 순서가 같음"
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "가용 영역마다 사설 서브넷 하나의 대역"
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_cidrs) > 0
    error_message = "사설 서브넷이 하나 이상 필요합니다."
  }
}

variable "enable_flow_logs" {
  description = "VPC Flow Logs를 CloudWatch에 남길지. 데모 기본값은 끔(비용)"
  type        = bool
  default     = false
}

variable "flow_log_retention_days" {
  description = "Flow Logs 로그 그룹 보존 기간(일)"
  type        = number
  default     = 7
}

variable "cluster_name" {
  description = "서브넷에 붙일 EKS 클러스터 이름(내부 로드밸런서 발견 태그). 없으면 null"
  type        = string
  default     = null
}

variable "tags" {
  description = "추가 태그. component 태그는 provider default_tags가 붙임"
  type        = map(string)
  default     = {}
}
