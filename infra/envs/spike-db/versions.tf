terraform {
  required_version = "~> 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71"
    }
  }

  # 상태: S3 백엔드, 키 envs/spike-db.tfstate. backend.hcl은 gitignore(계정 · 버킷 이름).
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  # NFR-8: 모든 자원에 component 태그. 스파이크 자원은 component=spike로 따로 셈.
  default_tags {
    tags = {
      component  = "spike"
      env        = "spike-db"
      managed-by = "terraform"
      spike      = "db-migration-matrix"
    }
  }
}
