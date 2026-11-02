# 플랫폼 노드(스펙 §7.2 "EC2 1대 + RKE2 단일 노드", T7). 사설 서브넷, 공인 IP 없음, 인바운드 규칙 없음.
# 운영자 접근은 SSM 세션(포트 포워딩)뿐이고 SSH 키를 두지 않음. IMDSv2만, hop limit 1이라 Pod가 노드 역할을
# 쓰지 못함. 노드 역할은 SSM 관리만 받고, 플랫폼 Pod의 AWS 권한은 서비스 계정 웹 신원으로 따로 받음(rke2.tf).

data "aws_ssm_parameter" "ami" {
  name = var.ami_ssm_parameter
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_kms_key" "root" {
  description             = "${var.name} 노드 루트 디스크(RKE2, etcd, local-path 볼륨)"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "root" {
  name          = "alias/${var.name}-node"
  target_key_id = aws_kms_key.root.key_id
}

resource "aws_security_group" "node" {
  name_prefix = "${var.name}-node-"
  description = "${var.name} RKE2 node: no inbound; outbound only (Cloudflare tunnel, SSM, registries, AWS APIs)"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-node" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.node.id
  description       = "outbound (NAT): Cloudflare 7844/443, SSM, ECR, GitHub, STS"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

data "aws_iam_policy_document" "assume_ec2" {
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
  description        = "${var.name} RKE2 node: SSM session only"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# 플랫폼 이미지는 같은 계정 ECR(bravo-platform/*)에 digest로 있음. containerd가 노드 역할로 당겨 옴(읽기만).
resource "aws_iam_role_policy_attachment" "ecr_pull" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
}

resource "aws_iam_instance_profile" "node" {
  name_prefix = "${var.name}-node-"
  role        = aws_iam_role.node.name
}

resource "aws_instance" "node" {
  ami                         = nonsensitive(data.aws_ssm_parameter.ami.value) # public Canonical AMI ID, not secret
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.node.id]
  iam_instance_profile        = aws_iam_instance_profile.node.name
  associate_public_ip_address = false
  user_data                   = var.user_data
  # The node is stateful (etcd, OpenBao, CNPG volumes): a cloud-init change does not replace it by itself.
  # Apply a changed drop-in on the live node (SSM) or replace deliberately: terraform apply -replace=<this resource>.
  user_data_replace_on_change = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    encrypted             = true
    kms_key_id            = aws_kms_key.root.arn
    delete_on_termination = true
    tags                  = merge(var.tags, { Name = "${var.name}-node-root" })
  }

  monitoring = false

  tags = merge(var.tags, { Name = "${var.name}-node" })

  lifecycle {
    # 새 Ubuntu AMI가 나와도 노드를 갈아 끼우지 않음. 교체는 판단해서 taint로.
    ignore_changes = [ami, user_data]
  }
}
