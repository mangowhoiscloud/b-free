terraform {
  required_version = "~> 1.17.0"
  # No cloud provider: the on-prem destination is Lima VMs driven by limactl through terraform_data
  # (NFR-7: apply creates, destroy removes). Requires limactl, jq, yq, kubectl on the laptop.
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
  }
}
