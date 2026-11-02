# 출발지 채팅 앱이 기대는 AWS 관리형 서비스. S1 · S2의 재료(스펙 §7.3, §8.10).
# - 첨부: S3 버킷. 공개 접근 차단 넷을 모두 켬. 데모 S1의 통제 차이는 demo_weak_bucket_policy로 만듦.
# - DB 비밀번호: Secrets Manager 비밀. Terraform은 그릇만 만들고 값을 넣지 않음(infra/README.md 규칙).
# - 키: 앱 데이터용 KMS CMK 하나(S3 SSE-KMS, Secrets Manager).
# - 권한: EKS Pod Identity로 서비스 계정마다 역할 하나. 노드 역할에는 앱 권한이 없음.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  app_namespace       = "chat"
  app_service_account = "chat-app"
  eso_namespace       = "external-secrets"
  eso_service_account = "external-secrets"

  # 데모는 destroy 뒤 같은 이름으로 다시 올림. 그래서 비밀을 바로 지움. 운영은 30일.
  secret_recovery_days = 0
}

# ---------------------------------------------------------------- KMS

resource "aws_kms_key" "app" {
  description             = "${var.name} 앱 데이터(S3 첨부, Secrets Manager)"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  # 키 정책은 기본값(계정 루트에 위임). 실제 사용 권한은 아래 IAM 정책이 kms:ViaService로 좁힘.
}

resource "aws_kms_alias" "app" {
  name          = "alias/${var.name}-app"
  target_key_id = aws_kms_key.app.key_id
}

# ---------------------------------------------------------------- S3 첨부 버킷

resource "aws_s3_bucket" "attachments" {
  bucket_prefix = "${var.name}-attachments-"
  force_destroy = true # 데모 환경. 운영은 false
}

resource "aws_s3_bucket_public_access_block" "attachments" {
  bucket = aws_s3_bucket.attachments.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "attachments" {
  bucket = aws_s3_bucket.attachments.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "attachments" {
  bucket = aws_s3_bucket.attachments.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "attachments" {
  bucket = aws_s3_bucket.attachments.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.app.arn
    }
    bucket_key_enabled = true
  }
}

# 강한 정책(기본): TLS가 아닌 요청 거부, 객체 동작은 앱 역할과 지정한 관리자만.
# 약한 정책(demo_weak_bucket_policy = true): TLS 강제 없음, 계정 루트에 s3:* 허용.
# 약한 정책도 공개 정책이 아니라서 공개 접근 차단에 걸리지 않음. S1에서 게이트가 이 차이를 잡음.
data "aws_iam_policy_document" "attachments_strong" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.attachments.arn, "${aws_s3_bucket.attachments.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyObjectAccessExceptApp"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.attachments.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = concat([aws_iam_role.chat_app.arn], var.bucket_admin_principal_arns)
    }
  }
}

data "aws_iam_policy_document" "attachments_weak" {
  statement {
    sid       = "DemoWeakAccountWideAccess"
    effect    = "Allow"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.attachments.arn, "${aws_s3_bucket.attachments.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_s3_bucket_policy" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  policy = var.demo_weak_bucket_policy ? data.aws_iam_policy_document.attachments_weak.json : data.aws_iam_policy_document.attachments_strong.json

  # 공개 접근 차단을 먼저 걸어 정책이 잠깐이라도 공개 상태가 되지 않게 함.
  depends_on = [aws_s3_bucket_public_access_block.attachments]
}

# ---------------------------------------------------------------- Secrets Manager

# 그릇만 만듦. 값은 클러스터 안에서 CNPG가 만들고 ESO PushSecret이 씀. 또는 운영자가 한 번
# aws secretsmanager put-secret-value로 넣음. 값이 Terraform 상태에 남지 않음(NFR-6).
resource "aws_secretsmanager_secret" "db_app" {
  name                    = "${var.name}/chat/db-app"
  description             = "채팅 앱 DB 접속 비밀번호(앱이 SDK로 직접 읽음, 스펙 §8.10)"
  kms_key_id              = aws_kms_key.app.arn
  recovery_window_in_days = local.secret_recovery_days
}

# ---------------------------------------------------------------- Pod Identity 역할

data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# 채팅 앱: 첨부 버킷 하나, 비밀 하나, 앱 키 하나.
resource "aws_iam_role" "chat_app" {
  name_prefix        = "${var.name}-chat-app-"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

data "aws_iam_policy_document" "chat_app" {
  statement {
    sid       = "ListAttachments"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.attachments.arn]
  }

  statement {
    sid       = "ReadWriteAttachments"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.attachments.arn}/*"]
  }

  statement {
    sid       = "ReadDbSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_app.arn]
  }

  statement {
    sid       = "UseAppKeyViaS3AndSecretsManager"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.app.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values = [
        "s3.${var.region}.amazonaws.com",
        "secretsmanager.${var.region}.amazonaws.com",
      ]
    }
  }
}

resource "aws_iam_role_policy" "chat_app" {
  name   = "chat-app"
  role   = aws_iam_role.chat_app.id
  policy = data.aws_iam_policy_document.chat_app.json
}

resource "aws_eks_pod_identity_association" "chat_app" {
  cluster_name    = module.cluster.name
  namespace       = local.app_namespace
  service_account = local.app_service_account
  role_arn        = aws_iam_role.chat_app.arn
}

# External Secrets: 이 환경 접두어(<name>/) 아래 비밀만 읽음. DB 비밀 하나에만 PushSecret으로 씀.
resource "aws_iam_role" "external_secrets" {
  name_prefix        = "${var.name}-eso-"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid       = "ReadEnvSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:${local.partition}:secretsmanager:${var.region}:${local.account_id}:secret:${var.name}/*"]
  }

  statement {
    sid       = "PushDbSecretValue"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [aws_secretsmanager_secret.db_app.arn]
  }

  statement {
    sid       = "UseAppKeyViaSecretsManager"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.app.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "external-secrets"
  role   = aws_iam_role.external_secrets.id
  policy = data.aws_iam_policy_document.external_secrets.json
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  cluster_name    = module.cluster.name
  namespace       = local.eso_namespace
  service_account = local.eso_service_account
  role_arn        = aws_iam_role.external_secrets.arn
}
