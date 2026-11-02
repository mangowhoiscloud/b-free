# 플랫폼(중앙 제어) 환경 = VPC ②(스펙 §7.2, T1). 클러스터는 이 VPC의 EC2 1대 + RKE2 단일 노드(rke2.tf, T7).
# 2026-11-02 류지환 결정 "스펙 그대로 전부"로 임시 플랫폼 EKS를 지움(같은 날 20:3x 생성, 21:4x 삭제).
# 플랫폼 묶음은 deploy/platform/ 에 있고 Argo CD가 Git에서 당겨 옴.

data "aws_availability_zones" "this" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(sort(data.aws_availability_zones.this.names), 0, 3)

  tags = merge(var.tags, {
    component  = "platform"
    env        = "platform-aws"
    managed-by = "terraform"
  })
}

module "network" {
  source = "../../modules/network/aws"

  name                 = var.name
  cidr                 = "10.20.0.0/16"
  azs                  = local.azs
  private_subnet_cidrs = ["10.20.11.0/24", "10.20.12.0/24", "10.20.13.0/24"]
  cluster_name         = var.name
}
