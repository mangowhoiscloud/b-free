terraform {
  required_version = "~> 1.17.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.9"
    }
  }

  # 상태: AWS 쪽 스파이크와 같은 S3 버킷, 키 envs/spike-db-gcp.tfstate(backend.hcl, gitignore).
  backend "s3" {}
}

provider "google" {
  project = var.project_id
  region  = var.region

  # NFR-8: component 라벨. 스파이크 자원은 component=spike로 따로 셈.
  default_labels = {
    component  = "spike"
    env        = "spike-db"
    managed-by = "terraform"
    spike      = "db-migration-matrix"
  }
}
