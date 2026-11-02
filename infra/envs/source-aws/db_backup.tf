# CNPG chat-db 백업 저장소(DR-01, 게이트 DR-RESTORE · DR-ARCHIVE). 첨부 버킷과 다른 버킷.
# - 버킷: WAL과 base backup(barman-cloud, deploy/landing-zone/overlays/eks/chat-db-backup.yaml)을 받음.
#   공개 접근 차단 넷, BucketOwnerEnforced, SSE-KMS(앱 키, 버킷 키), TLS 아닌 요청 거부.
# - 권한: EKS Pod Identity. 정적 AWS 키 없음(D-26). CNPG가 만드는 서비스 계정 chat/chat-db는 읽기 · 쓰기,
#   복구 검증 클러스터 chat/chat-db-restore는 읽기만.

resource "aws_s3_bucket" "db_backup" {
  bucket_prefix = "${var.name}-db-backup-"
  force_destroy = true # 데모 환경. 운영은 false
}

resource "aws_s3_bucket_public_access_block" "db_backup" {
  bucket = aws_s3_bucket.db_backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "db_backup" {
  bucket = aws_s3_bucket.db_backup.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "db_backup" {
  bucket = aws_s3_bucket.db_backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.app.arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "db_backup_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.db_backup.arn, "${aws_s3_bucket.db_backup.arn}/*"]
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
}

resource "aws_s3_bucket_policy" "db_backup" {
  bucket     = aws_s3_bucket.db_backup.id
  policy     = data.aws_iam_policy_document.db_backup_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.db_backup]
}

# chat/chat-db: barman-cloud-wal-archive, barman-cloud-backup, 보존 정책 삭제.
resource "aws_iam_role" "chat_db_backup" {
  name_prefix        = "${var.name}-chat-db-backup-"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

data "aws_iam_policy_document" "chat_db_backup" {
  statement {
    sid       = "ListBackupBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.db_backup.arn]
  }

  statement {
    sid       = "ReadWriteBackupObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.db_backup.arn}/*"]
  }

  statement {
    sid       = "UseAppKeyViaS3"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.app.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "chat_db_backup" {
  name   = "chat-db-backup"
  role   = aws_iam_role.chat_db_backup.id
  policy = data.aws_iam_policy_document.chat_db_backup.json
}

resource "aws_eks_pod_identity_association" "chat_db_backup" {
  cluster_name    = module.cluster.name
  namespace       = local.app_namespace
  service_account = "chat-db"
  role_arn        = aws_iam_role.chat_db_backup.arn
}

# chat/chat-db-restore: 복구(barman-cloud-restore, barman-cloud-wal-restore)만. 쓰기 없음.
resource "aws_iam_role" "chat_db_restore" {
  name_prefix        = "${var.name}-chat-db-restore-"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

data "aws_iam_policy_document" "chat_db_restore" {
  statement {
    sid       = "ListBackupBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.db_backup.arn]
  }

  statement {
    sid       = "ReadBackupObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.db_backup.arn}/*"]
  }

  statement {
    sid       = "DecryptAppKeyViaS3"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.app.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "chat_db_restore" {
  name   = "chat-db-restore"
  role   = aws_iam_role.chat_db_restore.id
  policy = data.aws_iam_policy_document.chat_db_restore.json
}

resource "aws_eks_pod_identity_association" "chat_db_restore" {
  cluster_name    = module.cluster.name
  namespace       = local.app_namespace
  service_account = "chat-db-restore"
  role_arn        = aws_iam_role.chat_db_restore.arn
}

output "db_backup_bucket" {
  description = "CNPG chat-db 백업 버킷(barman-cloud). 첨부 버킷과 다름"
  value       = aws_s3_bucket.db_backup.bucket
}

output "db_backup_role_arns" {
  description = "EKS Pod Identity 역할(chat/chat-db 읽기 · 쓰기, chat/chat-db-restore 읽기)"
  value = {
    "chat/chat-db"         = aws_iam_role.chat_db_backup.arn
    "chat/chat-db-restore" = aws_iam_role.chat_db_restore.arn
  }
}
