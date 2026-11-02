terraform {
  required_version = "~> 1.17.0"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.28"
    }
  }
  # 상태는 다른 환경과 같은 S3 백엔드(key = "envs/edge.tfstate"). terraform init -backend-config=backend.hcl
  backend "s3" {}
}

# D-26: API 토큰은 플랫폼 OpenBao에서 이 프로세스에만 넘김.
#   deploy/pilot/scripts/keys.sh with-cloudflare -- terraform plan
provider "cloudflare" {}
