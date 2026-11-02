# 플랫폼 이미지 저장소(T7). 이미지는 digest로만 배포함(§8.8). 태그는 덮어쓸 수 없음.
# 스펙의 GHCR 대신 같은 계정 ECR: 노드가 노드 역할(AmazonEC2ContainerRegistryPullOnly)로 당겨 와서
# 클러스터에 pull 토큰이 필요 없음. GHCR 공개 패키지 안(D-14)은 제안 상태.

resource "aws_ecr_repository" "platform" {
  for_each = toset(var.platform_images)

  name                 = "${var.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # 데모 환경. destroy 때 이미지와 함께 지움. 운영은 false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "platform" {
  for_each   = aws_ecr_repository.platform
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
