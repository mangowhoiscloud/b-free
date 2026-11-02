# GitHub Actions OIDC 배포 신원(T7, NFR-6). 장기 키 없이 한 저장소 · 한 ref의 워크플로만 역할을 받음.
# 권한은 지정한 ECR 저장소에 이미지 push뿐. 클러스터 접근은 주지 않음(배포는 Argo CD pull, D-16).

locals {
  oidc_host = "token.actions.githubusercontent.com"
  # sub 클레임 형식: repo:<owner>/<repo>:ref:<ref>(GitHub 문서 "About security hardening with OpenID Connect").
  subject = "repo:${var.github_repository}:ref:${var.github_ref}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  tags           = var.tags
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1

  url = "https://${local.oidc_host}"
}

locals {
  provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "GitHubActionsOneRepoOneRef"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    # 정확히 같은 값만(StringEquals). StringLike와 와일드카드를 쓰지 않음.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [local.subject]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name_prefix          = "${var.name}-"
  description          = "GitHub Actions OIDC: ${local.subject} -> ECR push only"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = var.max_session_duration
  tags                 = var.tags
}

data "aws_iam_policy_document" "ecr_push" {
  # GetAuthorizationToken은 자원 수준 권한이 없음(ECR 문서). 토큰만으로는 어떤 저장소에도 쓰지 못함.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushToPlatformRepositoriesOnly"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
    ]
    resources = var.ecr_repository_arns
  }
}

resource "aws_iam_role_policy" "ecr_push" {
  name   = "ecr-push"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.ecr_push.json
}
