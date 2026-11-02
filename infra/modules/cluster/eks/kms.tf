# 고객 관리 키 둘: K8s Secret 봉투 암호화용, EBS(노드 루트 디스크와 PV)용.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region
  # 관리형 노드 그룹은 Auto Scaling 그룹으로 인스턴스를 띄움.
  # CMK로 암호화한 루트 볼륨을 붙이려면 Auto Scaling 서비스 연결 역할이 키를 써야 함(EC2 Auto Scaling 문서).
  autoscaling_slr = "arn:${local.partition}:iam::${local.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
}

resource "aws_kms_key" "secrets" {
  description             = "${var.name} EKS Secret 봉투 암호화"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  tags                    = var.tags
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${var.name}-eks-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

data "aws_iam_policy_document" "ebs_key" {
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid = "AutoScalingUseForEncryptedRootVolumes"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    # 새 계정에는 이 역할이 첫 Auto Scaling 그룹 전까지 없어, 주체로 적으면 키 생성이 거부됨.
    # 그래서 주체는 "*"로 두고 같은 계정의 그 역할로만 조건을 좁힘.
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalArn"
      values   = [local.autoscaling_slr]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "AutoScalingCreateGrantForAwsResources"
    actions   = ["kms:CreateGrant"]
    resources = ["*"]
    # 새 계정에는 이 역할이 첫 Auto Scaling 그룹 전까지 없어, 주체로 적으면 키 생성이 거부됨.
    # 그래서 주체는 "*"로 두고 같은 계정의 그 역할로만 조건을 좁힘.
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalArn"
      values   = [local.autoscaling_slr]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "ebs" {
  description             = "${var.name} EBS(노드 루트 디스크, bravo-db, bravo-general)"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.ebs_key.json
  tags                    = var.tags
}

resource "aws_kms_alias" "ebs" {
  name          = "alias/${var.name}-ebs"
  target_key_id = aws_kms_key.ebs.key_id
}
