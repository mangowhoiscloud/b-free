variable "account_id" {
  type = string
}

variable "zone_id" {
  type = string
}

variable "app" {
  type    = string
  default = "chat"
}

variable "environments" {
  type = map(object({
    origin_service = string
    weight         = number
  }))
}

variable "lb_enabled" {
  type    = bool
  default = false
}

variable "active_env" {
  type    = string
  default = "src"
}
