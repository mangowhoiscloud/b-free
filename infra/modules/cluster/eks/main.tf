# EKS 제어면. API는 비공개 엔드포인트만 둠(02-network.md 3절). 밖에서는 링크 `*-k8s`로 닿음.

resource "aws_cloudwatch_log_group" "cluster" {
  # EKS가 스스로 만들면 destroy 뒤에 남음(01-environments.md 6절). 미리 만들어 Terraform이 지우게 함.
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = var.control_plane_log_retention_days
  tags              = var.tags
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  # 기본 자체 관리 add-on(aws-node, kube-proxy, CoreDNS)을 깔지 않음. 아래 관리형 add-on으로만 둠.
  bootstrap_self_managed_addons = false
  deletion_protection           = var.deletion_protection

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    # 기본은 비공개 전용(D-16). 링크 · CodeBuild 부트스트랩이 서기 전 첫 설치 동안만
    # 운영자 IP 하나에 공개 엔드포인트를 열고, 끝나면 bootstrap_public_cidrs를 비워 다시 닫음.
    endpoint_public_access = length(var.bootstrap_public_cidrs) > 0
    public_access_cidrs    = length(var.bootstrap_public_cidrs) > 0 ? var.bootstrap_public_cidrs : null
  }

  kubernetes_network_config {
    ip_family         = "ipv4"
    service_ipv4_cidr = var.service_cidr
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.secrets.arn
    }
  }

  # 표준 지원이 끝나도 확장 지원 요금으로 넘어가지 않게 함. 판을 올리라는 신호로 씀.
  upgrade_policy {
    support_type = "STANDARD"
  }

  tags = var.tags

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy.cluster_secrets_kms,
    aws_cloudwatch_log_group.cluster,
  ]
}

resource "aws_eks_access_entry" "admin" {
  for_each = toset(var.admin_principal_arns)

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  type          = "STANDARD"
  tags          = var.tags
}

resource "aws_eks_access_policy_association" "admin" {
  for_each = toset(var.admin_principal_arns)

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.admin]
}
