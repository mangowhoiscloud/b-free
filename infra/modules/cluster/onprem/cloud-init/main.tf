# Common RKE2 node cloud-init (spec T2). Consumers: infra/modules/cluster/onprem (Lima VMs) and the
# platform EC2 node (user_data = module.<name>.user_data). No secret is rendered.
terraform {
  required_version = "~> 1.17.0"
}

variable "role" {
  description = "server or agent."
  type        = string
  validation {
    condition     = contains(["server", "agent"], var.role)
    error_message = "role must be server or agent."
  }
}

variable "rke2" {
  description = "RKE2 release and the SHA-256 of rke2.linux-<arch>.tar.gz from that release's sha256sum-<arch>.txt."
  type = object({
    version = string
    sha256  = string
  })
}

variable "arch" {
  description = "arm64 or amd64 (must match rke2.sha256)."
  type        = string
  default     = "arm64"
}

variable "node_name" {
  description = "Kubernetes node name; empty keeps the hostname."
  type        = string
  default     = ""
}

variable "tls_sans" {
  description = "Extra API server certificate names (server only). The node IP is added at boot."
  type        = list(string)
  default     = ["127.0.0.1", "localhost"]
}

variable "cis_preconditions" {
  description = <<-EOT
    Write the CIS host conditions (kernel sysctls, etcd user and group). Keep true. false is only for the
    spec S1 exception 4a run (docs/evidence/s1-cis-refusal): RKE2 with profile: cis then refuses to start.
  EOT
  type        = bool
  default     = true
}

locals {
  user_data = templatefile("${path.module}/rke2-node.yaml.tftpl", {
    role              = var.role
    rke2_version      = var.rke2.version
    rke2_sha256       = var.rke2.sha256
    arch              = var.arch
    node_name         = var.node_name
    tls_sans          = var.tls_sans
    cis_preconditions = var.cis_preconditions
  })
}

output "user_data" {
  description = "#cloud-config document (under the 16 KiB EC2 user_data limit)."
  value       = local.user_data
}
