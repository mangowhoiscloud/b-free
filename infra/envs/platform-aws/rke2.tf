# 플랫폼 클러스터 = VPC ②의 EC2 1대 + RKE2 단일 노드(스펙 §7.2, T7. 류지환 결정 2026-11-02 21:1x).
# - 노드 준비는 온프렘과 같은 공통 cloud-init(infra/modules/cluster/onprem/cloud-init, RKE2 CIS 프로필, Cilium).
# - 플랫폼 Pod의 AWS 권한: 서비스 계정 웹 신원. RKE2의 서비스 계정 발급자를 이 계정의 S3 공개 문서 두 개
#   (OIDC discovery, JWKS: 공개 키뿐)로 내놓고, IAM OIDC 공급자 + 서비스 계정마다 역할(AssumeRoleWithWebIdentity).
#   스펙 T7 "출발지 · 목적지 접근 역할(수집용 읽기 / Executor 제어용 분리)". 노드 역할에는 이 권한이 없음.

locals {
  rke2 = {
    version = "v1.36.5+rke2r1"
    # sha256sum-arm64.txt of the release, rke2.linux-arm64.tar.gz (checked 2026-11-02)
    sha256 = "244454bed11d9525452e10a2e5055da5d5fc6b61928ca3fae778bc4254ce467f"
  }
  oidc_bucket = "${var.name}-oidc-${data.aws_caller_identity.current.account_id}"
  oidc_issuer = "https://${local.oidc_bucket}.s3.${var.region}.amazonaws.com"
  oidc_host   = "${local.oidc_bucket}.s3.${var.region}.amazonaws.com"

  # 서비스 계정(namespace:name) → 역할. 수집 역할(읽기)과 Executor 역할(제어)을 나눔.
  # group: 출발지 EKS access entry의 K8s 그룹(null이면 출발지 권한 없음).
  web_identity_roles = {
    collector-ro = { sa = "system:serviceaccount:bravo-control:collector", group = "bravo-collector" }
    executor-ctl = { sa = "system:serviceaccount:bravo-control:executor", group = "bravo-executor" }
    ebs-csi      = { sa = "system:serviceaccount:kube-system:ebs-csi-controller-sa", group = null }
    ecr-pull     = { sa = "system:serviceaccount:bravo-control:ecr-pull-refresher", group = null }
  }
  source_roles = var.source_cluster_name == null ? {} : { for k, v in local.web_identity_roles : k => v if v.group != null }
}

data "aws_caller_identity" "current" {}

data "aws_eks_cluster" "source" {
  count = var.source_cluster_name == null ? 0 : 1
  name  = var.source_cluster_name
}

data "aws_iam_policy_document" "describe_source" {
  count = var.source_cluster_name == null ? 0 : 1

  statement {
    sid       = "DescribeSourceClusterOnly"
    actions   = ["eks:DescribeCluster"]
    resources = [data.aws_eks_cluster.source[0].arn]
  }
}

module "rke2_cloud_init" {
  source = "../../modules/cluster/onprem/cloud-init"

  role      = "server"
  rke2      = local.rke2
  arch      = "arm64"
  node_name = var.name
  tls_sans  = ["127.0.0.1", "localhost"]
}

# 플랫폼만의 추가 하나: 서비스 계정 토큰의 발급자를 S3 공개 문서 주소로(웹 신원용). 공통 문서는 한 글자도 바꾸지 않고
# 끝에 bootcmd 키 하나만 덧붙임(공통 문서에 없는 키). MIME 여러 부분으로 나누면 cloud-init이 한국어 주석이 든
# 부분을 us-ascii로 읽다 버림(2026-11-02 21:16 실측 "unacceptable character #xdca7"), 그래서 한 문서로 둠.
locals {
  # 30-platform.yaml(RKE2 config drop-in, 공통 config.yaml 위에 얹힘):
  # - cni: none: CNI는 랜딩 존과 같은 Cilium 1.20.4 + Hubble을 플랫폼이 깜(deploy/platform/cilium/values-rke2.yaml).
  # - 서비스 계정 발급자 = S3 공개 discovery 주소(웹 신원).
  # - provider-id · zone 라벨: EBS CSI가 IMDS 없이(hop limit 1) 노드의 인스턴스와 AZ를 앎.
  # - bravo.io/pool=core: 플랫폼 매니페스트의 nodeSelector(단일 노드라 이 노드가 core).
  platform_dropin_script = <<-EOT
    set -eu
    install -d -m 0700 /etc/rancher/rke2/config.yaml.d
    T=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
    IID=$(curl -sf -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/instance-id)
    AZ=$(curl -sf -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/placement/availability-zone)
    cat > /etc/rancher/rke2/config.yaml.d/30-platform.yaml <<EOF
    cni: none
    kube-apiserver-arg+:
      - service-account-issuer=${local.oidc_issuer}
      - service-account-jwks-uri=${local.oidc_issuer}/openid/v1/jwks
    kubelet-arg+:
      - provider-id=aws:///$AZ/$IID
    node-label+:
      - topology.kubernetes.io/region=${var.region}
      - topology.kubernetes.io/zone=$AZ
      - bravo.io/pool=core
    EOF
    chmod 0600 /etc/rancher/rke2/config.yaml.d/30-platform.yaml
  EOT
  platform_bootcmd       = yamlencode({ bootcmd = [["sh", "-c", local.platform_dropin_script]] })
  node_user_data         = "${trimspace(module.rke2_cloud_init.user_data)}\n${local.platform_bootcmd}"
}

module "node" {
  source = "../../modules/node/aws"

  name          = var.name
  vpc_id        = module.network.vpc_id
  subnet_id     = module.network.private_subnet_ids_by_az[local.azs[0]]
  instance_type = var.node_instance_type
  user_data     = local.node_user_data
  tags          = local.tags
}

# --- 서비스 계정 웹 신원(OIDC discovery + JWKS는 공개 키뿐, 이 두 객체만 공개 읽기)

resource "aws_s3_bucket" "oidc" {
  bucket        = local.oidc_bucket
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "oidc" {
  bucket = aws_s3_bucket.oidc.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "oidc" {
  bucket                  = aws_s3_bucket.oidc.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false # 아래 정책이 discovery 문서 두 개만 공개함
  restrict_public_buckets = false
}

data "aws_iam_policy_document" "oidc_public" {
  statement {
    sid       = "PublicReadDiscoveryAndJwksOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.oidc.arn}/.well-known/openid-configuration", "${aws_s3_bucket.oidc.arn}/openid/v1/jwks"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "oidc" {
  bucket     = aws_s3_bucket.oidc.id
  policy     = data.aws_iam_policy_document.oidc_public.json
  depends_on = [aws_s3_bucket_public_access_block.oidc]
}

# discovery 문서는 발급자 주소만으로 정해짐(kube-apiserver의 /.well-known/openid-configuration과 같은 내용).
# IAM은 공급자를 만들 때 이 문서를 읽으므로 먼저 올림. JWKS(공개 키)는 노드가 키를 만든 뒤
# deploy/platform/scripts/publish-sa-jwks.sh 가 클러스터에서 읽어 올림(키가 바뀌면 다시 실행).
resource "aws_s3_object" "oidc_discovery" {
  bucket       = aws_s3_bucket.oidc.id
  key          = ".well-known/openid-configuration"
  content_type = "application/json"
  content = jsonencode({
    issuer                                = local.oidc_issuer
    jwks_uri                              = "${local.oidc_issuer}/openid/v1/jwks"
    response_types_supported              = ["id_token"]
    subject_types_supported               = ["public"]
    id_token_signing_alg_values_supported = ["RS256"]
  })
}

resource "aws_iam_openid_connect_provider" "rke2" {
  url            = local.oidc_issuer
  client_id_list = ["sts.amazonaws.com"]
  depends_on     = [aws_s3_object.oidc_discovery, aws_s3_bucket_policy.oidc]
}

data "aws_iam_policy_document" "web_identity" {
  for_each = local.web_identity_roles

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.rke2.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [each.value.sa]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "web_identity" {
  for_each = local.web_identity_roles

  name_prefix        = "${var.name}-${each.key}-"
  description        = "RKE2 service account ${each.value.sa} (web identity)"
  assume_role_policy = data.aws_iam_policy_document.web_identity[each.key].json
}

resource "aws_iam_role_policy" "web_identity_describe_source" {
  for_each = local.source_roles

  name   = "describe-source-eks"
  role   = aws_iam_role.web_identity[each.key].id
  policy = data.aws_iam_policy_document.describe_source[0].json
}

# 출발지 EKS의 K8s 권한은 그룹에만 묶음(deploy/platform/link/source-rbac): 수집 = 읽기 ClusterRole,
# Executor = ns chat 의 exec · port-forward Role.
resource "aws_eks_access_entry" "source_web_identity" {
  for_each = local.source_roles

  cluster_name      = var.source_cluster_name
  principal_arn     = aws_iam_role.web_identity[each.key].arn
  type              = "STANDARD"
  kubernetes_groups = [each.value.group]
}

# EBS CSI 컨트롤러(kube-system/ebs-csi-controller-sa): bravo-db · bravo-general 볼륨. AWS 관리형 정책
# AmazonEBSCSIDriverPolicy(자기가 만든 볼륨 태그 조건) + 노드 KMS 키 하나를 EC2 경유로만.
resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.web_identity["ebs-csi"].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

data "aws_iam_policy_document" "ebs_csi_kms" {
  statement {
    actions   = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
    resources = [module.node.kms_key_arn]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
  statement {
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = [module.node.kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ec2.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ebs_csi_kms" {
  name   = "node-kms-via-ec2"
  role   = aws_iam_role.web_identity["ebs-csi"].id
  policy = data.aws_iam_policy_document.ebs_csi_kms.json
}

# 플랫폼 이미지 당기기(bravo-platform/* digest): RKE2 containerd에는 ECR 자격 플러그인이 없음. 클러스터 안 CronJob이
# 이 역할로 12시간짜리 ECR 토큰을 받아 Secret bravo-control/ecr-pull(dockerconfigjson)을 갱신(deploy/platform/ecr-pull).
# 토큰으로 할 수 있는 일은 이 역할의 권한(플랫폼 저장소 읽기)뿐.
data "aws_iam_policy_document" "ecr_pull" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [for r in aws_ecr_repository.platform : r.arn]
  }
}

resource "aws_iam_role_policy" "ecr_pull" {
  name   = "platform-ecr-pull"
  role   = aws_iam_role.web_identity["ecr-pull"].id
  policy = data.aws_iam_policy_document.ecr_pull.json
}
