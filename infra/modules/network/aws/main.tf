# 사설 서브넷만 두는 VPC. 아웃바운드는 Regional NAT Gateway 하나로 나감(제안 D-16).
# Regional NAT는 공개 서브넷 없이 IGW만 있으면 동작함(aws provider 6.71 nat_gateway 문서, auto mode).

locals {
  subnets = { for i, az in var.azs : az => var.private_subnet_cidrs[i] }

  cluster_tags = var.cluster_name == null ? {} : {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/internal-elb"           = "1"
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = var.name })
}

# 기본 보안 그룹의 규칙을 모두 비움. 쓰지 않는 SG가 열려 있지 않게 함(CIS AWS 5.4).
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-default-closed" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = var.name })
}

resource "aws_subnet" "private" {
  for_each = local.subnets

  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = each.value
  map_public_ip_on_launch = false

  tags = merge(var.tags, local.cluster_tags, {
    Name = "${var.name}-private-${each.key}"
    tier = "private"
  })
}

# auto mode: 워크로드 ENI가 생긴 AZ로 자동 확장하고 EIP도 자동으로 붙임.
resource "aws_nat_gateway" "regional" {
  vpc_id            = aws_vpc.this.id
  availability_mode = "regional"
  connectivity_type = "public"

  tags = merge(var.tags, { Name = "${var.name}-regional-nat" })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-private" })
}

resource "aws_route" "private_default" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.regional.id
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# S3 게이트웨이 엔드포인트. 첨부 복사(F8)와 이미지 계층 일부가 NAT 데이터 처리 비용 없이 나감.
data "aws_region" "current" {}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = merge(var.tags, { Name = "${var.name}-s3" })
}

resource "aws_cloudwatch_log_group" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  name              = "/bravo/vpc/${var.name}/flow"
  retention_in_days = var.flow_log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "flow_assume" {
  count = var.enable_flow_logs ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  name_prefix        = "${var.name}-flow-"
  assume_role_policy = data.aws_iam_policy_document.flow_assume[0].json
  tags               = var.tags
}

data "aws_iam_policy_document" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  role   = aws_iam_role.flow[0].id
  policy = data.aws_iam_policy_document.flow[0].json
}

resource "aws_flow_log" "this" {
  count = var.enable_flow_logs ? 1 : 0

  vpc_id          = aws_vpc.this.id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.flow[0].arn
  iam_role_arn    = aws_iam_role.flow[0].arn
  tags            = var.tags
}
