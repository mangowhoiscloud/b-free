# 출발지(blue) 클러스터. EKS, 앱 노드는 첫 AZ 하나(01-environments.md 1절).
# EKS 제어면은 서로 다른 AZ의 서브넷 둘이 필요해서 셋째 AZ 서브넷을 하나 더 둠(EKS 요건).
# 출발지의 AZ 장애 내성은 주장하지 않음(02-network.md 2절).

data "aws_availability_zones" "this" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  names = sort(data.aws_availability_zones.this.names)
  azs   = [local.names[0], local.names[min(2, length(local.names) - 1)]]

  presets = {
    small = {
      app_instance  = ["t4g.large"]
      app_max       = 2
      storage_quota = "50Gi"
    }
    medium = {
      app_instance  = ["t4g.xlarge"]
      app_max       = 4
      storage_quota = "200Gi"
    }
  }
  preset = local.presets[var.size]

  node_pools = {
    app = {
      instance_types = local.preset.app_instance
      azs            = [local.azs[0]]
      min_per_az     = 1
      desired_per_az = 1
      max_per_az     = local.preset.app_max
    }
  }

  kubeconfig_path = "${abspath(path.root)}/.terraform/bravo/kubeconfig"
  gitops_dir      = "${abspath(path.root)}/gitops"

  tags = merge(var.tags, {
    component  = "source"
    env        = "source-aws"
    managed-by = "terraform"
  })
}

module "network" {
  source = "../../modules/network/aws"

  name                 = var.name
  cidr                 = "10.10.0.0/16"
  azs                  = local.azs
  private_subnet_cidrs = ["10.10.11.0/24", "10.10.13.0/24"]
  cluster_name         = var.name
}

module "cluster" {
  source = "../../modules/cluster/eks"

  name                   = var.name
  kubernetes_version     = "1.36"
  vpc_id                 = module.network.vpc_id
  subnet_ids             = module.network.private_subnet_ids
  subnet_ids_by_az       = module.network.private_subnet_ids_by_az
  service_cidr           = "172.20.0.0/16"
  node_pools             = local.node_pools
  kubeconfig_path        = local.kubeconfig_path
  bootstrap_public_cidrs = var.bootstrap_public_cidrs
  admin_principal_arns   = var.admin_principal_arns
  # 시작 템플릿의 tag_specifications에는 provider default_tags가 붙지 않음. NFR-8 component 태그를 직접 넘김.
  tags = local.tags
}
