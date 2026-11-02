# 플랫폼 게이트(platform/gate, FR-6 · FR-12)의 출발지 AWS 읽기 신원. 수집기와 다른 역할(gate-ro, rke2.tf의
# web_identity_roles): 서비스 계정 bravo-control/gate의 웹 신원. 출발지 EKS 권한은 access entry 그룹 bravo-gate에만
# 묶음(deploy/platform/link/source-rbac/gate.yaml). 아래 권한은 게이트 질문이 읽는 것뿐이고 쓰기는 없음.
#   OBJ-ENC   s3:GetEncryptionConfiguration(첨부 버킷), kms:DescribeKey
#   OBJ-04    앱 자격 없이 iam:SimulatePrincipalPolicy(앱 역할)에 넣을 버킷 정책 · 키 정책: s3:GetBucketPolicy, kms:GetKeyPolicy
#   OBJ-01/06 s3:GetObject는 canary 키 하나만(출발지 버킷 정책의 canary 예외 canary_reader_principal_arns와 짝)
#   SEC-*     secretsmanager:DescribeSecret · GetResourcePolicy, iam:GetRole · SimulatePrincipalPolicy, EKS encryptionConfig
#   AUD-*     cloudtrail:DescribeTrails · GetTrailStatus · GetEventSelectors, eks:DescribeCluster(rke2.tf)
data "aws_iam_policy_document" "gate_read" {
  count = var.source_attachments_bucket == null ? 0 : 1

  statement {
    sid       = "AttachmentBucketEncryptionAndPolicyFacts"
    actions   = ["s3:GetEncryptionConfiguration", "s3:GetBucketPolicy"]
    resources = ["arn:aws:s3:::${var.source_attachments_bucket}"]
  }
  statement {
    sid       = "CanaryObjectOnly"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.source_attachments_bucket}/_gate/canary"]
  }
  statement {
    sid       = "KeyAndSecretFacts"
    actions   = ["kms:DescribeKey", "kms:GetKeyPolicy", "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy"]
    resources = ["*"]
  }
  statement {
    sid       = "PolicySimulationAndAudit"
    actions   = ["iam:GetRole", "iam:SimulatePrincipalPolicy", "cloudtrail:DescribeTrails", "cloudtrail:GetTrailStatus", "cloudtrail:GetEventSelectors"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gate_read" {
  count = var.source_attachments_bucket == null ? 0 : 1

  name   = "gate-source-read"
  role   = aws_iam_role.web_identity["gate-ro"].id
  policy = data.aws_iam_policy_document.gate_read[0].json
}

# OBJ-05 "다른 자격": 같은 게이트 서비스 계정이 가장하는 두 번째 역할. 정책도 access entry도 없음. 출발지 버킷 정책의
# 객체 거부에 걸려 canary GET이 AccessDenied여야 PASS(게이트는 AWS_CONFIG_FILE 프로필 gate-other로 씀).
resource "aws_iam_role" "gate_other" {
  name_prefix        = "${var.name}-gate-other-"
  description        = "RKE2 service account system:serviceaccount:bravo-control:gate, no permissions (gate OBJ-05)"
  assume_role_policy = data.aws_iam_policy_document.web_identity["gate-ro"].json
}

output "gate_other_role_arn" {
  description = "OBJ-05의 다른 자격(권한 없음)"
  value       = aws_iam_role.gate_other.arn
}

output "gate_role_arn" {
  description = "플랫폼 게이트 읽기 역할(출발지 버킷 정책 canary_reader_principal_arns에 넣음)"
  value       = aws_iam_role.web_identity["gate-ro"].arn
}
