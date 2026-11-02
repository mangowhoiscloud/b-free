variable "name" {
  description = "클러스터 이름. 예: bravo-platform"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,38}$", var.name))
    error_message = "소문자, 숫자, 하이픈으로 3–39자여야 합니다."
  }
}

variable "kubernetes_version" {
  description = "K8s minor. 모든 환경 공통 1.36(09-cni-csi.md)"
  type        = string
  default     = "1.36"
}

variable "vpc_id" {
  description = "network/aws의 vpc_id"
  type        = string
}

variable "subnet_ids" {
  description = "제어면 ENI를 둘 사설 서브넷. 서로 다른 AZ 둘 이상(EKS 요건)"
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "EKS는 서로 다른 AZ의 서브넷 둘 이상이 필요합니다."
  }
}

variable "subnet_ids_by_az" {
  description = "AZ 이름에서 사설 서브넷 ID로. 노드 그룹을 AZ마다 하나씩 만들 때 씀"
  type        = map(string)
}

variable "service_cidr" {
  description = "Service 대역. 기본값에 맡기지 않고 명시함(01-environments.md 4절)"
  type        = string
}

variable "node_pools" {
  description = <<-EOT
    역할별 노드 풀. AZ마다 관리형 노드 그룹 하나를 만들어 EBS 볼륨의 AZ와 노드 AZ를 맞춤.
    taints의 effect는 NO_SCHEDULE, NO_EXECUTE, PREFER_NO_SCHEDULE 중 하나.
  EOT
  type = map(object({
    instance_types = list(string)
    azs            = list(string)
    min_per_az     = number
    desired_per_az = number
    max_per_az     = number
    disk_gib       = optional(number, 40)
    labels         = optional(map(string), {})
    taints = optional(list(object({
      key    = string
      value  = string
      effect = string
    })), [])
  }))
}

variable "ami_type" {
  description = "관리형 노드 그룹 AMI 종류. t4g(arm64)라서 AL2023 arm64"
  type        = string
  default     = "AL2023_ARM_64_STANDARD"
}

variable "addon_versions" {
  description = "관리형 add-on 판 고정. 비우면 이 K8s 판의 기본 판(data aws_eks_addon_version)을 씀"
  type        = map(string)
  default     = {}
}

variable "network_policy_engine" {
  description = <<-EOT
    NetworkPolicy를 집행하는 엔진. cilium 또는 vpc-cni.
    cilium: landing zone이 Cilium을 VPC CNI chaining 모드로 깜. VPC CNI는 IPAM만 하고 정책 에이전트는 끔.
    vpc-cni: Cilium 없이 VPC CNI 네트워크 정책 에이전트로 집행함(폴백).
  EOT
  type        = string
  default     = "cilium"

  validation {
    condition     = contains(["cilium", "vpc-cni"], var.network_policy_engine)
    error_message = "cilium 또는 vpc-cni만 받습니다."
  }
}

variable "admin_principal_arns" {
  description = "클러스터 관리자 access entry를 줄 IAM 주체(CI OIDC 역할 등). 생성자는 따로 자동으로 받음"
  type        = list(string)
  default     = []
}

variable "control_plane_log_retention_days" {
  description = "제어면 로그 보존 기간(일)"
  type        = number
  default     = 30
}

variable "deletion_protection" {
  description = "클러스터 삭제 보호. 데모 환경은 destroy를 위해 끔. 운영은 true"
  type        = bool
  default     = false
}

variable "kubeconfig_path" {
  description = "kubeconfig를 쓸 경로. 비밀이 없음(aws eks get-token exec 방식)"
  type        = string
}

variable "tags" {
  description = "추가 태그"
  type        = map(string)
  default     = {}
}

variable "bootstrap_public_cidrs" {
  description = "첫 설치 동안만 EKS API를 열 운영자 IP(/32). 비우면 비공개 전용(기본, D-16). 설치가 끝나면 반드시 비움"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.bootstrap_public_cidrs : can(regex("/32$", c))])
    error_message = "운영자 IP 하나(/32)만 받습니다."
  }
}
