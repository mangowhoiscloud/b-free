# 플랫폼 클러스터(중앙 제어). EKS, 3 AZ. 01-environments.md 2절의 층 구분을 노드 풀로 옮김.
# 제어면은 EKS가 3 AZ에 둠. core 풀은 AZ마다 1대, agents 풀은 셋째 AZ에 taint를 걸어 둠.

data "aws_availability_zones" "this" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  azs = slice(sort(data.aws_availability_zones.this.names), 0, 3)

  presets = {
    small = {
      core_instance   = ["t4g.large"]
      core_max_per_az = 2
      agent_instance  = ["t4g.large"]
      agent_max       = 2
      storage_quota   = "100Gi"
    }
    medium = {
      core_instance   = ["t4g.xlarge"]
      core_max_per_az = 3
      agent_instance  = ["t4g.xlarge"]
      agent_max       = 4
      storage_quota   = "300Gi"
    }
  }
  preset = local.presets[var.size]

  node_pools = {
    core = {
      instance_types = local.preset.core_instance
      azs            = local.azs
      min_per_az     = 1
      desired_per_az = 1
      max_per_az     = local.preset.core_max_per_az
    }
    agents = {
      instance_types = local.preset.agent_instance
      azs            = [local.azs[2]]
      min_per_az     = 1
      desired_per_az = 1
      max_per_az     = local.preset.agent_max
      taints = [{
        key    = "pool"
        value  = "agents"
        effect = "NO_SCHEDULE"
      }]
    }
  }

  # 비밀이 없는 kubeconfig. .terraform/ 아래라 Git에 올라가지 않음.
  kubeconfig_path = "${abspath(path.root)}/.terraform/bravo/kubeconfig"
  gitops_dir      = "${abspath(path.root)}/gitops"

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

module "cluster" {
  source = "../../modules/cluster/eks"

  name               = var.name
  kubernetes_version = "1.36"
  vpc_id             = module.network.vpc_id
  subnet_ids         = module.network.private_subnet_ids
  subnet_ids_by_az   = module.network.private_subnet_ids_by_az
  service_cidr       = "10.63.0.0/16"
  node_pools         = local.node_pools
  kubeconfig_path    = local.kubeconfig_path
  # 시작 템플릿의 tag_specifications에는 provider default_tags가 붙지 않음. NFR-8 component 태그를 직접 넘김.
  tags = local.tags
}

# 고객 클러스터와 같은 landing zone 묶음(deploy/landing-zone). eks overlay는 EBS 기반 StorageClass,
# Cilium chaining 값, 노드 선택만 다름. Argo CD Application 파일만 쓰고 클러스터에 적용하지 않음.
module "landing_zone" {
  source = "../../modules/landing_zone"

  provider_kind = module.cluster.storage_platform
  delivery      = module.cluster.addon_delivery
  argocd = {
    repo_url        = var.gitops_repo_url
    target_revision = "main"
    path            = "infra/envs/platform-aws/gitops"
  }
  output_dir = local.gitops_dir
  vars = {
    cluster_name  = module.cluster.name
    region        = var.region
    kms_key_arn   = module.cluster.ebs_kms_key_arn
    storage_quota = local.preset.storage_quota
  }
}
