variable "enabled" {
  description = <<-EOT
    Cloudflare Zero Trust(Access)를 켠 뒤에만 true. false면 이 모듈은 아무 자원도 만들지 않음.
    Access 앱 없이 링크 호스트를 내놓지 않기 위한 게이트(D-16, 02-network.md 4.2 "순서").
  EOT
  type        = bool
  default     = false
}

variable "account_id" {
  description = "Cloudflare 계정 ID"
  type        = string
}

variable "zone_id" {
  description = "링크 호스트를 둘 영역 ID"
  type        = string
}

variable "tenant" {
  description = "테넌트 ID. 호스트 · Tunnel 이름의 앞머리(02-network.md 4.1). 예: t1"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,15}$", var.tenant))
    error_message = "소문자로 시작하는 1–16자 영숫자여야 합니다."
  }
}

variable "environment_id" {
  description = "서비스를 내놓는 고객 환경 ID(CLOUD-E2E-CONTRACT.md 2절). 예: source-aws, target-onprem"
  type        = string
}

variable "environment_short" {
  description = "이름에 쓰는 환경 약칭(02-network.md 4.1). 예: src, dst"
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2,8}$", var.environment_short))
    error_message = "소문자 2–8자여야 합니다."
  }
}

variable "links" {
  description = <<-EOT
    내놓는 링크. 키는 서비스 약칭(k8s, db, pgb). service는 노출하는 클러스터 안의 원본(tcp://이름:포트).
    clients는 이 링크를 쓰는 소비 환경 약칭(plat, src, dst). 서비스 토큰 이름은 <client>-<environment_short>-<키>.
  EOT
  type = map(object({
    service = string
    clients = list(string)
  }))

  validation {
    condition     = alltrue([for l in values(var.links) : can(regex("^tcp://[a-z0-9.-]+:[0-9]+$", l.service))])
    error_message = "service는 tcp://<이름>:<포트> 형식이어야 합니다."
  }
}

variable "service_token_ids" {
  description = <<-EOT
    링크 키 → 그 Access 앱의 Service Auth 정책이 받을 서비스 토큰 ID 목록. ID는 비밀이 아님.
    토큰(Client ID · Secret)은 Terraform이 만들지 않음. 플랫폼 키 단계(deploy/pilot/scripts/keys.sh)가
    Cloudflare API로 만들어 플랫폼 OpenBao에만 씀(D-26). enabled = true면 모든 링크에 하나 이상 있어야 함.
  EOT
  type        = map(list(string))
  default     = {}
}

variable "client_environment_ids" {
  description = "소비 환경 약칭 → 환경 ID. 토큰을 둘 OpenBao 경로(secret/envs/<환경 ID>/…)를 정함"
  type        = map(string)
  default = {
    plat = "platform"
    src  = "source-aws"
    dst  = "target-onprem"
  }
}

variable "session_duration" {
  description = "Access 앱 세션 길이. 서비스 토큰 연결에 적용"
  type        = string
  default     = "24h"
}
