variable "name" {
  description = "VPC 이름 접두어. 예: bravo-target-gcp"
  type        = string
}

variable "project_id" {
  description = "GCP 프로젝트 ID"
  type        = string
}

variable "region" {
  description = "서브넷과 Cloud NAT 리전. 예: asia-northeast3"
  type        = string
}

variable "node_cidr" {
  description = "노드 서브넷 대역(01-environments.md 4절)"
  type        = string
  default     = "10.30.0.0/24"
}

variable "pod_cidr" {
  description = "Pod 보조 대역"
  type        = string
  default     = "10.72.0.0/16"
}

variable "service_cidr" {
  description = "Service 보조 대역"
  type        = string
  default     = "10.73.0.0/16"
}

variable "enable_flow_logs" {
  description = "서브넷 Flow Logs. 기본 끔(비용)"
  type        = bool
  default     = false
}
