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

  # 가장 작은 크기(2026-11-02 류지환 지시 "가장 작은 노드"): core는 t4g.large.
  # t4g.medium은 VPC CNI 기본(접두사 위임 없음)에서 노드당 Pod 17개라 플랫폼 Pod(약 45개)를 3대에 담지 못하고,
  # 접두사 위임(노드당 110)을 켜면 AL2023 kube-reserved가 11 MiB × 110 + 255 MiB ≈ 1.4 GiB라 할당 가능 메모리가 노드당 약 2.3 GiB로 줄어듦.
  # agents는 에이전트 Pod 전용(taint)이라 데몬셋과 Trial 하나를 담는 t4g.small.
  presets = {
    small = {
      core_instance   = ["t4g.large"]
      core_max_per_az = 2
      agent_instance  = ["t4g.small"]
      agent_max       = 2
      disk_gib        = 20
      storage_quota   = "100Gi"
    }
    medium = {
      core_instance   = ["t4g.xlarge"]
      core_max_per_az = 3
      agent_instance  = ["t4g.xlarge"]
      agent_max       = 4
      disk_gib        = 40
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
      disk_gib       = local.preset.disk_gib
    }
    agents = {
      instance_types = local.preset.agent_instance
      azs            = [local.azs[2]]
      min_per_az     = 1
      desired_per_az = 1
      max_per_az     = local.preset.agent_max
      disk_gib       = local.preset.disk_gib
      taints = [{
        key    = "pool"
        value  = "agents"
        effect = "NO_SCHEDULE"
      }]
    }
  }

  # 비밀이 없는 kubeconfig. .terraform/ 아래라 Git에 올라가지 않음.
  kubeconfig_path = "${abspath(path.root)}/.terraform/bravo/kubeconfig"

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
  # 첫 설치 동안만 운영자 IP 하나(/32)에 공개 엔드포인트를 엶. 설치가 끝나면 비움(14-provisioning-scenarios.md 1절 3).
  bootstrap_public_cidrs = var.bootstrap_public_cidrs
  admin_principal_arns   = var.admin_principal_arns
  # 시작 템플릿의 tag_specifications에는 provider default_tags가 붙지 않음. NFR-8 component 태그를 직접 넘김.
  tags = local.tags
}

# 플랫폼 묶음(Cilium chaining, Argo CD, CNPG, OpenBao, 관측, 플랫폼 서비스)은 Terraform이 아니라
# deploy/platform/ 을 Argo CD가 당겨 와서 설치함(D-16, 14-provisioning-scenarios.md 1절 4–5).
# modules/landing_zone 은 아직 없어서 여기서 부르지 않음(source-aws 의 landing_zone.tf.off 와 같은 상태).
