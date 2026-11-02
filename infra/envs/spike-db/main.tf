# DB 이전 매트릭스 스파이크(docs/architecture/12-db-migration-matrix.md 11절, D-24).
# 출발지 VPC(source-aws)의 사설 서브넷에 가장 싼 RDS for PostgreSQL 16 하나를 둡니다.
#
# - 단일 AZ, db.t4g.micro, gp3 20 GiB, 자동 백업 없음, 삭제 보호 없음. 스파이크가 끝나면 terraform destroy.
# - 마스터 비밀번호는 RDS가 만들고 Secrets Manager에 둡니다(manage_master_user_password).
#   Terraform 상태와 출력에는 비밀 ARN만 있고 값은 없습니다(D-26).
# - TLS 강제: rds.force_ssl = 1. 공개 접근 없음.
# - 논리 디코딩: var.logical_replication. static이라 재부팅해야 켜짐(SPK-01).
#
# 네트워크: EKS 클러스터 보안 그룹(노드와 VPC CNI 파드가 씀)에서 오는 5432만 받습니다.
# 역방향(RDS가 구독자)에서는 RDS가 같은 VPC의 링크 파드로 나가야 하므로, 클러스터 보안 그룹에
# "스파이크 RDS 보안 그룹 → TCP 5432" 규칙 하나를 더합니다. 이 규칙도 이 상태가 소유하고 destroy 때 지웁니다.

data "aws_vpc" "source" {
  tags = { Name = var.source_name }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.source.id]
  }
  tags = { tier = "private" }
}

data "aws_eks_cluster" "source" {
  name = var.source_name
}

locals {
  cluster_sg_id = data.aws_eks_cluster.source.vpc_config[0].cluster_security_group_id
}

resource "aws_db_subnet_group" "spike" {
  name        = "${var.identifier}-subnets"
  description = "bravo spike RDS, source VPC private subnets"
  subnet_ids  = data.aws_subnets.private.ids
}

resource "aws_security_group" "rds" {
  name        = "${var.identifier}-sg"
  description = "bravo spike RDS: 5432 from the source EKS cluster only"
  vpc_id      = data.aws_vpc.source.id
  tags        = { Name = "${var.identifier}-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_cluster" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL from EKS nodes and VPC CNI pods (link pod, PgBouncer)"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = local.cluster_sg_id
}

# RDS가 구독자일 때(역방향) 링크 파드로 나가는 길.
resource "aws_vpc_security_group_egress_rule" "rds_to_cluster" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Subscriber connections from RDS to the in-VPC link pod"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = local.cluster_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "cluster_from_rds" {
  security_group_id            = local.cluster_sg_id
  description                  = "spike-db: RDS subscriber to link pod 5432 (destroy with spike-db)"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_db_parameter_group" "spike" {
  name        = "${var.identifier}-pg16"
  family      = "postgres16"
  description = "bravo spike: TLS required, logical replication toggle"

  parameter {
    name         = "rds.logical_replication"
    value        = var.logical_replication ? "1" : "0"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  parameter {
    name         = "max_slot_wal_keep_size"
    value        = tostring(var.max_slot_wal_keep_size_mb)
    apply_method = "immediate"
  }

  parameter {
    name         = "password_encryption"
    value        = "scram-sha-256"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_replication_commands"
    value        = "1"
    apply_method = "immediate"
  }
}

resource "aws_db_instance" "spike" {
  identifier     = var.identifier
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "chat"
  username = "bravo_master"
  # 비밀번호는 RDS가 만들고 Secrets Manager에 둠. 상태에는 비밀 ARN만 남음.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.spike.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false
  parameter_group_name   = aws_db_parameter_group.spike.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period      = 0
  skip_final_snapshot          = true
  deletion_protection          = false
  apply_immediately            = true
  auto_minor_version_upgrade   = false
  performance_insights_enabled = false
  monitoring_interval          = 0

  tags = { Name = var.identifier }
}
