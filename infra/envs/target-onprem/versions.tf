terraform {
  required_version = "~> 1.17.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
  }

  # Local state on the laptop that runs the Lima VMs (the VMs exist only here). The state holds
  # instance names, versions and hashes only: no token, password or key (D-26).
  backend "local" {
    path = "terraform.tfstate"
  }
}
