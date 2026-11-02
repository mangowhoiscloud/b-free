terraform {
  required_version = "~> 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
  }

  # 상태는 S3 백엔드 + 잠금(infra/README.md 규칙). 버킷 이름은 저장소에 두지 않고 init 때 넘김.
  #   terraform init -backend-config=backend.hcl
  # backend.hcl 예: bucket = "...", key = "envs/source-aws.tfstate", region = "ap-northeast-2", use_lockfile = true
  # 검증만 할 때는 terraform init -backend=false
  backend "s3" {}
}

provider "aws" {
  region = var.region

  # NFR-8: 모든 자원에 component 태그
  default_tags {
    tags = local.tags
  }
}
