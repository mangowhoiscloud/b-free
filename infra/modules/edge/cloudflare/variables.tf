variable "account_id" {
  description = "Cloudflare 계정 ID"
  type        = string
}

variable "zone_id" {
  description = "사용자 진입 이름을 둘 영역 ID"
  type        = string
}

variable "app" {
  description = "앱 진입 이름의 앞부분. 환경별 이름은 <app>-<env>.<zone>, LB 이름은 <app>.<zone>"
  type        = string
}

variable "environments" {
  description = "환경별 터널 대상. origin_service는 그 클러스터 안 Gateway 주소(cf-edge가 당겨 쓰는 원격 설정)"
  type = map(object({
    origin_service = string
    weight         = number # LB 첫 가중치. 이후 가중치는 콘솔 · Executor가 바꿈(D-10)
  }))
}

variable "lb_enabled" {
  description = "Cloudflare Load Balancing 구독이 있을 때만 true"
  type        = bool
  default     = false
}

variable "lb_environments" {
  description = "LB pool을 만들 환경 이름 목록. null이면 environments 전부. 요금제의 포함 끝점 수를 넘지 않게 고름"
  type        = list(string)
  default     = null
}

variable "health_path" {
  description = "LB 모니터가 확인할 경로"
  type        = string
  default     = "/healthz"
}

variable "active_env" {
  description = "LB 없이 쓸 때 대표 이름(<app>.<zone>)이 처음 가리킬 환경. 이후 전환은 Executor가 API로 함"
  type        = string
  default     = "src"
}
