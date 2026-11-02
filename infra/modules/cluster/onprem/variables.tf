variable "name" {
  description = "Environment prefix for Lima instances and disks (e.g. bravo-onprem)."
  type        = string
}

variable "image" {
  description = "Ubuntu cloud image for the nodes, pinned by release URL and SHA-256."
  type = object({
    url    = string
    sha256 = string
  })
}

variable "k3s" {
  description = "K3s release and the SHA-256 of k3s-arm64 from that release's sha256sum-arm64.txt."
  type = object({
    version = string
    sha256  = string
  })
}

variable "nodes" {
  description = "Node sizes. Keys are fixed: server (control plane, embedded etcd) and agent."
  type = map(object({
    cpus      = number
    memory    = string
    disk      = string
    data_disk = string
  }))
  validation {
    condition     = length(var.nodes) == 2 && contains(keys(var.nodes), "server") && contains(keys(var.nodes), "agent")
    error_message = "nodes must have exactly the keys server and agent."
  }
}

variable "api_host_port" {
  description = "Laptop port (127.0.0.1) that Lima forwards to the K3s API on the server VM."
  type        = number
  default     = 16443
}

variable "kube_context" {
  description = "kubectl context name merged into ~/.kube/config."
  type        = string
}
