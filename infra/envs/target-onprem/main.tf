# target-onprem: the on-prem destination (spec FR-3, §7.2, T2) = Ubuntu VMs + RKE2 (CIS profile)
# on two Lima VMs, installed by the common cloud-init, + the landing zone (D-23, overlay rke2).
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

  # Same minor as the running EKS (1.36). sha256: rke2.linux-arm64.tar.gz in the release's
  # sha256sum-arm64.txt (v1.36.5+rke2r1: Kubernetes v1.36.5, etcd v3.6.14-k3s3, rke2-cilium 1.20.200).
  rke2 = {
    version = "v1.36.5+rke2r1"
    sha256  = "244454bed11d9525452e10a2e5055da5d5fc6b61928ca3fae778bc4254ce467f"
  }

  nodes         = var.nodes
  api_host_port = var.api_host_port
}

# Landing zone (deploy/landing-zone, overlay rke2): Cilium + Hubble, StorageClasses, CNPG, chat app,
# cf-edge, policies, storage. Secrets come from the platform OpenBao through pipes (keys.sh), never
# through Terraform.
resource "terraform_data" "landing_zone" {
  count = var.landing_zone ? 1 : 0

  triggers_replace = [module.cluster.kube_context, module.cluster.instances]

  provisioner "local-exec" {
    command = "'${abspath("${path.root}/../../../deploy/landing-zone/install.sh")}' rke2 ${module.cluster.kube_context}"
    environment = {
      BRAVO_LIMA_SERVER = module.cluster.instances.server
      BRAVO_LIMA_NODES  = "${module.cluster.instances.server} ${module.cluster.instances.agent}"
    }
  }
}
