terraform {
  required_version = "~> 1.17.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.9"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # 상태는 GCS 백엔드(잠금 내장). 버킷 이름은 저장소에 두지 않고 init 때 넘김.
  #   terraform init -backend-config=backend.hcl
  # backend.hcl 예: bucket = "...", prefix = "envs/target-gcp"
  # 검증만 할 때는 terraform init -backend=false
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region

  # NFR-8: 모든 자원에 component 라벨(GCP 라벨은 소문자만)
  default_labels = merge(var.labels, {
    component  = "target"
    env        = "target-gcp"
    managed-by = "terraform"
  })
}
