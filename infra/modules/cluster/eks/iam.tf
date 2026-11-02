# 제어면 역할, 노드 역할, EBS CSI 컨트롤러 역할(EKS Pod Identity).

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name_prefix        = "${var.name}-cluster-"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

data "aws_iam_policy_document" "cluster_secrets_kms" {
  statement {
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ListGrants",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.secrets.arn]
  }
}

resource "aws_iam_role_policy" "cluster_secrets_kms" {
  name   = "secrets-kms"
  role   = aws_iam_role.cluster.id
  policy = data.aws_iam_policy_document.cluster_secrets_kms.json
}

data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name_prefix        = "${var.name}-node-"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
  tags               = var.tags
}

# 노드 역할은 최소 셋만. VPC CNI와 EBS CSI 권한은 노드가 아니라 add-on 역할로 줌.
# AmazonEKS_CNI_Policy는 노드에 둠: aws-node는 hostNetwork Pod라 Pod Identity 대신 노드 역할을 씀(기본 구성).
resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEC2ContainerRegistryPullOnly",
    "AmazonEKS_CNI_Policy",
  ])

  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name_prefix        = "${var.name}-ebs-csi-"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# EBS CSI 문서의 KMS 정책. 이 클러스터의 EBS 키 하나에만, EC2 경유 호출에만 허용.
data "aws_iam_policy_document" "ebs_csi_kms" {
  statement {
    sid = "CreateGrantForEbsOnly"
    actions = [
      "kms:CreateGrant",
      "kms:ListGrants",
      "kms:RevokeGrant",
    ]
    resources = [aws_kms_key.ebs.arn]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ec2.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid = "UseKeyViaEc2Only"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.ebs.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ec2.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role_policy" "ebs_csi_kms" {
  name   = "ebs-kms"
  role   = aws_iam_role.ebs_csi.id
  policy = data.aws_iam_policy_document.ebs_csi_kms.json
}
