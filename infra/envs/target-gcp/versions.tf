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

  # 상태는 다른 환경과 같은 S3 백엔드(플랫폼 계정, 잠금 파일). key = "envs/target-gcp.tfstate".
  #   terraform init -backend-config=backend.hcl
  # 왜 GCS가 아닌가(README "상태 저장"): 상태는 플랫폼이 쥐는 것이고(TECH-STACK-SPEC 3절 #1), 지울 수도 있는
  # 고객 프로젝트 안에 두지 않음. 경계는 S3 API라 endpoint만 바꾸면 RGW 등 S3 호환 저장소로 옮길 수 있음.
  # 검증만 할 때는 terraform init -backend=false
  backend "s3" {}
}

provider "google" {
  project = var.project_id
  region  = var.region

  # NFR-8: 모든 자원에 component 라벨(GCP 라벨은 소문자만)
  default_labels = local.labels
}
