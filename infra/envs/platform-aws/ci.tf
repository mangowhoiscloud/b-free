# GitHub Actions OIDC(T7, NFR-6): mangowhoiscloud/b-free 의 main 워크플로만 역할을 받고,
# 플랫폼 ECR 저장소에 이미지를 push하는 것만 함. 계정에 공급자가 이미 있어 찾아 씀.

module "ci_identity" {
  source = "../../modules/ci_identity/aws"

  name                 = "${var.name}-ci"
  github_repository    = var.github_repository
  github_ref           = var.github_ref
  create_oidc_provider = false
  ecr_repository_arns  = [for r in aws_ecr_repository.platform : r.arn]
  tags                 = local.tags
}
