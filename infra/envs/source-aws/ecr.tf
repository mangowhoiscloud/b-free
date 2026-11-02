# 출발지 고객의 이미지 저장소(CLOUD-E2E-CONTRACT §5): 고객이 원래 쓰던 레지스트리라는 설정.
# 이미지는 digest로만 배포함(§8.8). 태그는 덮어쓸 수 없음. 푸시할 때마다 기본 스캔.
# 노드 역할에 AmazonEC2ContainerRegistryPullOnly가 있어 클러스터에 pull 토큰이 필요 없음.
# KMS: 저장소 전용 CMK. ECR이 저장소를 만들 때 키에 grant를 걸어 쓰므로 노드 역할에 KMS 권한을 더하지 않음.
# component=source 태그는 provider default_tags(local.tags)가 붙임(NFR-8).

locals {
  source_images = ["bravo-demo", "bravo-pgbouncer"]
}

resource "aws_kms_key" "ecr" {
  description             = "${var.name} 컨테이너 이미지(ECR)"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/${var.name}-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}

resource "aws_ecr_repository" "source" {
  for_each = toset(local.source_images)

  name                 = "${var.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # 데모 환경. destroy 때 이미지와 함께 지움. 운영은 false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ecr.arn
  }
}

resource "aws_ecr_lifecycle_policy" "source" {
  for_each   = aws_ecr_repository.source
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the newest 30 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

output "ecr_repositories" {
  description = "출발지 이미지 저장소(이름 → URL)"
  value       = { for k, r in aws_ecr_repository.source : k => r.repository_url }
}

output "ebs_kms_key_arn" {
  description = "EBS CMK. StorageClass bravo-db · bravo-general(deploy/cloud/source-aws)이 씀"
  value       = module.cluster.ebs_kms_key_arn
}
