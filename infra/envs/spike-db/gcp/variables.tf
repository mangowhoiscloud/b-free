variable "project_id" {
  description = "GCP 프로젝트. 항상 명시(gcloud 기본 프로젝트를 쓰지 않음)"
  type        = string
}

variable "region" {
  type    = string
  default = "asia-northeast1"
}

variable "zone" {
  type    = string
  default = "asia-northeast1-a"
}

variable "name" {
  type    = string
  default = "bravo-spike-db"
}

variable "tier" {
  description = "가장 싼 Cloud SQL 등급(공유 코어)"
  type        = string
  default     = "db-f1-micro"
}

variable "logical_decoding" {
  description = "cloudsql.logical_decoding. 처음에는 false로 만들고 켤 때 재시작 시간을 잼(SPK-01과 같은 방식)"
  type        = bool
  default     = false
}

variable "availability_type" {
  description = "ZONAL(기본). SPK-07: REGIONAL로 바꿔 HA 인스턴스가 구독자가 될 수 없는지 확인"
  type        = string
  default     = "ZONAL"
}

variable "enable_alloydb" {
  description = "AlloyDB 클러스터 · 인스턴스(SPK-11, 직접 연결 verify-full). 비용이 커서 기본 false"
  type        = bool
  default     = false
}

variable "quiet_error_statements" {
  description = "log_min_error_statement = panic: failed statements (passwords in CREATE ROLE / conninfo) are not logged"
  type        = bool
  default     = true
}
