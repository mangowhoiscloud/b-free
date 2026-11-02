# target-onprem: the on-prem destination (D-21) = K3s on two Lima VMs + the landing zone (D-23).
#   terraform init && terraform apply      # VMs, K3s, kubeconfig context, landing zone
#   terraform destroy                      # deletes the VMs, data disks and the context
# Versions below were checked against the official releases on 2026-11-02.

module "cluster" {
  source = "../../modules/cluster/onprem"

  name         = var.name
  kube_context = var.kube_context

  # Ubuntu 24.04 LTS arm64, release-20261017 (SHA256SUMS of that release).
  image = {
    url    = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-arm64.img"
    sha256 = "50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e"
  }

  # Same minor as the running EKS (1.36). sha256: k3s-arm64 in the release's sha256sum-arm64.txt.
  k3s = {
    version = "v1.36.5+k3s1"
    sha256  = "135e34cb9e8a1cfae3cb55577789e93501efe7edc080a8b2630458a85b07721e"
  }

  nodes         = var.nodes
  api_host_port = var.api_host_port
}

# Landing zone (deploy/landing-zone, overlay k3s): Cilium + Hubble, StorageClasses, CNPG, chat app,
# cf-edge, policies, storage. Secrets come from the platform OpenBao through pipes (keys.sh), never
# through Terraform.
resource "terraform_data" "landing_zone" {
  count = var.landing_zone ? 1 : 0

  triggers_replace = [module.cluster.kube_context, module.cluster.instances]

  provisioner "local-exec" {
    command = "'${abspath("${path.root}/../../../deploy/landing-zone/install.sh")}' k3s ${module.cluster.kube_context}"
  }
}
