variable "region" {
  description = "AWS 리전. 출발지 VPC(infra/envs/source-aws)와 같아야 함"
  type        = string
  default     = "ap-northeast-2"
}

variable "source_name" {
  description = "출발지 VPC · EKS 이름(infra/envs/source-aws var.name). 이 VPC의 사설 서브넷에 RDS를 둠"
  type        = string
  default     = "bravo-source"
}

variable "identifier" {
  description = "RDS 인스턴스 식별자"
  type        = string
  default     = "bravo-spike-rds"
}

variable "engine_version" {
  description = "RDS for PostgreSQL 판. 파일럿 CNPG · 컨테이너와 같은 16.15"
  type        = string
  default     = "16.15"
}

variable "instance_class" {
  description = "가장 싼 Graviton 인스턴스"
  type        = string
  default     = "db.t4g.micro"
}

variable "logical_replication" {
  description = <<-EOT
    rds.logical_replication. 처음에는 false(고객의 기존 DB를 흉내 냄)로 만들고,
    SPK-01에서 true로 바꾼 뒤 재부팅 시간을 잽니다. static 파라미터라 재부팅 전에는 적용되지 않습니다.
  EOT
  type        = bool
  default     = false
}

variable "max_slot_wal_keep_size_mb" {
  description = "슬롯이 붙잡을 수 있는 WAL 상한(MB). 03 4.1, 12 4.1절. -1이면 무제한"
  type        = number
  default     = 2048
}
