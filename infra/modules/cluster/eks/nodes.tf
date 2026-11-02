# 역할별 노드 풀을 AZ마다 관리형 노드 그룹 하나로 펼침.
# 그래서 AZ 하나의 노드가 교체돼도 같은 AZ에 다시 뜨고, 그 AZ의 EBS 볼륨을 다시 붙임(01-environments.md 2절).

locals {
  node_groups = merge([
    for pool, p in var.node_pools : {
      for az in p.azs : "${pool}-${az}" => merge(p, { pool = pool, az = az })
    }
  ]...)
}

resource "aws_launch_template" "node" {
  for_each = var.node_pools

  name_prefix            = "${var.name}-${each.key}-"
  update_default_version = true

  # IMDSv2 필수, hop limit 1. Pod(호스트 네트워크가 아닌)는 노드 역할 자격을 IMDS로 얻지 못함(02-network.md N14).
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = each.value.disk_gib
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.ebs.arn
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = false
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(var.tags, { Name = "${var.name}-${each.key}", "bravo.io/pool" = each.key })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = merge(var.tags, { Name = "${var.name}-${each.key}-root" })
  }

  tags = var.tags
}

resource "aws_eks_node_group" "this" {
  for_each = local.node_groups

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${each.value.pool}-${each.value.az}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [var.subnet_ids_by_az[each.value.az]]
  ami_type        = var.ami_type
  instance_types  = each.value.instance_types
  capacity_type   = "ON_DEMAND"

  scaling_config {
    min_size     = each.value.min_per_az
    desired_size = each.value.desired_per_az
    max_size     = each.value.max_per_az
  }

  update_config {
    max_unavailable = 1
  }

  node_repair_config {
    enabled = true
  }

  launch_template {
    id      = aws_launch_template.node[each.value.pool].id
    version = aws_launch_template.node[each.value.pool].latest_version
  }

  labels = merge(each.value.labels, {
    "bravo.io/pool" = each.value.pool
  })

  dynamic "taint" {
    for_each = each.value.taints
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  tags = var.tags

  # 노드가 Ready가 되려면 CNI와 kube-proxy가 먼저 있어야 함(bootstrap_self_managed_addons = false).
  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.before_nodes,
  ]

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}
