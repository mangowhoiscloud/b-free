# 관리형 add-on. 판은 addon_versions로 고정하고, 비우면 이 K8s 판의 기본 판을 씀.

locals {
  # Cilium chaining(Cilium 문서 "AWS VPC CNI plugin", 1.20.4, 2026-11-02 확인): VPC CNI가 ENI와 IPAM을 맡고
  # Cilium이 같은 veth에 eBPF를 붙여 정책과 Hubble을 맡음. 정책 엔진을 둘 두지 않으려고 VPC CNI 정책 에이전트를 끔.
  # 폴백(vpc-cni)일 때 키 이름은 AWS 문서 "Restrict Pod network traffic with Kubernetes network policies"를 따름.
  vpc_cni_config = (
    var.network_policy_engine == "cilium"
    ? jsonencode({ enableNetworkPolicy = "false" })
    : jsonencode({ enableNetworkPolicy = "true", nodeAgent = { enablePolicyEventLogs = "true" } })
  )

  # 노드보다 먼저 깔아야 노드가 Ready가 됨.
  addons_before_nodes = {
    "vpc-cni"                = local.vpc_cni_config
    "kube-proxy"             = null
    "eks-pod-identity-agent" = null
  }

  # 노드가 있어야 Pod가 뜸. 노드 그룹 뒤에 깜.
  addons_after_nodes = {
    "coredns"             = null
    "snapshot-controller" = null
  }

  all_addon_names = concat(
    keys(local.addons_before_nodes),
    keys(local.addons_after_nodes),
    ["aws-ebs-csi-driver"],
  )
}

data "aws_eks_addon_version" "default" {
  for_each = toset(local.all_addon_names)

  addon_name         = each.value
  kubernetes_version = aws_eks_cluster.this.version
  most_recent        = false
}

resource "aws_eks_addon" "before_nodes" {
  for_each = local.addons_before_nodes

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = lookup(var.addon_versions, each.key, data.aws_eks_addon_version.default[each.key].version)
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = var.tags
}

resource "aws_eks_addon" "after_nodes" {
  for_each = local.addons_after_nodes

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = lookup(var.addon_versions, each.key, data.aws_eks_addon_version.default[each.key].version)
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = var.tags

  depends_on = [aws_eks_node_group.this]
}

# EBS CSI 컨트롤러는 EKS Pod Identity로 역할을 받음. 노드 역할에는 EBS 권한이 없음.
resource "aws_eks_pod_identity_association" "ebs_csi" {
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"
  role_arn        = aws_iam_role.ebs_csi.arn
  tags            = var.tags

  depends_on = [aws_eks_addon.before_nodes]
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  addon_version               = lookup(var.addon_versions, "aws-ebs-csi-driver", data.aws_eks_addon_version.default["aws-ebs-csi-driver"].version)
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  # add-on 기본값은 기본 StorageClass를 만들지 않음. StorageClass는 landing zone이 bravo-db, bravo-general로 만듦.
  tags = var.tags

  depends_on = [
    aws_eks_node_group.this,
    aws_eks_pod_identity_association.ebs_csi,
    aws_iam_role_policy.ebs_csi_kms,
  ]
}
