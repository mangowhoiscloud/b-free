# 플랫폼 Pod의 AWS 자격(D-21, NFR-6): EKS Pod Identity. 노트북 ~/.aws 마운트를 대신함(D-26).
# 서비스 계정마다 역할 하나, 권한은 그 서비스가 부르는 API만.
#   bravo-control/collector  출발지 EKS DescribeCluster(엔드포인트 · CA). K8s 읽기는 출발지 access entry + RBAC(links.tf)
#   bravo-control/executor   출발지 EKS DescribeCluster. K8s는 ns chat 의 exec · port-forward 만(links.tf)
#   bravo-link/src-link      출발지 EKS DescribeCluster. ns chat 의 port-forward 만(임시 링크(D-16 전), links.tf)

data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

data "aws_eks_cluster" "source" {
  count = var.source_cluster_name == null ? 0 : 1
  name  = var.source_cluster_name
}

locals {
  source_cluster_arn = var.source_cluster_name == null ? null : data.aws_eks_cluster.source[0].arn

  # namespace/serviceaccount => 역할 키
  pod_roles = var.source_cluster_name == null ? {} : {
    collector = { namespace = "bravo-control", service_account = "collector" }
    executor  = { namespace = "bravo-control", service_account = "executor" }
    src-link  = { namespace = "bravo-link", service_account = "src-link" }
  }
}

resource "aws_iam_role" "pod" {
  for_each = local.pod_roles

  name_prefix        = "${var.name}-${each.key}-"
  description        = "EKS Pod Identity ${each.value.namespace}/${each.value.service_account}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

data "aws_iam_policy_document" "describe_source" {
  count = var.source_cluster_name == null ? 0 : 1

  statement {
    sid       = "DescribeSourceClusterOnly"
    actions   = ["eks:DescribeCluster"]
    resources = [local.source_cluster_arn]
  }
}

resource "aws_iam_role_policy" "describe_source" {
  for_each = local.pod_roles

  name   = "describe-source-eks"
  role   = aws_iam_role.pod[each.key].id
  policy = data.aws_iam_policy_document.describe_source[0].json
}

resource "aws_eks_pod_identity_association" "pod" {
  for_each = local.pod_roles

  cluster_name    = module.cluster.name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = aws_iam_role.pod[each.key].arn
}
