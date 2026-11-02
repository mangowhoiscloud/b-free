variable "name" {
  description = "Prefix of the Lima instances (bravo-onprem-server, bravo-onprem-agent)."
  type        = string
  default     = "bravo-onprem"
}

variable "kube_context" {
  description = "kubectl context merged into ~/.kube/config (CLOUD-E2E-CONTRACT §2)."
  type        = string
  default     = "bravo-onprem"
}

variable "api_host_port" {
  description = "Laptop port for the K3s API (Lima forwards 127.0.0.1:<port> to the server VM's 6443)."
  type        = number
  default     = 16443
}

variable "nodes" {
  description = "VM sizes. The agent carries Rook-Ceph OSD/RGW and the app; both have a raw data disk for one OSD."
  type = map(object({
    cpus      = number
    memory    = string
    disk      = string
    data_disk = string
  }))
  default = {
    server = { cpus = 4, memory = "8GiB", disk = "40GiB", data_disk = "30GiB" }
    agent  = { cpus = 6, memory = "14GiB", disk = "40GiB", data_disk = "30GiB" }
  }
}

variable "landing_zone" {
  description = "Run deploy/landing-zone/install.sh k3s after the cluster is up (false: cluster only)."
  type        = bool
  default     = true
}
