# On-prem destination cluster (spec FR-3, §7.2, T2): Ubuntu VMs + RKE2 with the CIS profile, two Lima
# VMs (server + agent) on this laptop, installed by the common cloud-init (./cloud-init, shared with
# the platform EC2 node). Lima has no Terraform provider, so each step is a terraform_data with a
# local-exec provisioner; apply creates, destroy deletes (NFR-7). No secret enters the state: the
# agent join token moves between the VMs through a pipe only (scripts/rke2-node.sh).

locals {
  scripts   = "${path.module}/scripts"
  lima_dir  = "${path.root}/.lima"
  instances = { for k, _ in var.nodes : k => "${var.name}-${k}" }
  lima_yaml = {
    for k, n in var.nodes : k => templatefile("${path.module}/files/lima-node.yaml.tftpl", {
      cpus          = n.cpus
      memory        = n.memory
      disk          = n.disk
      image_url     = var.image.url
      image_digest  = "sha256:${var.image.sha256}"
      data_disk     = "${local.instances[k]}-data"
      port_forwards = k == "server" ? [{ guest = 6443, host = var.api_host_port }] : []
    })
  }
  rke2_script_sha = filesha256("${local.scripts}/rke2-node.sh")
}

module "cloud_init" {
  for_each  = var.nodes
  source    = "./cloud-init"
  role      = each.key
  rke2      = var.rke2
  arch      = "arm64"
  node_name = local.instances[each.key]
}

resource "local_file" "lima_yaml" {
  for_each        = local.lima_yaml
  filename        = "${local.lima_dir}/${local.instances[each.key]}.yaml"
  content         = each.value
  file_permission = "0644"
}

# Rendered cloud-config per node (no secret in it).
resource "local_file" "cloud_init" {
  for_each        = var.nodes
  filename        = "${local.lima_dir}/${local.instances[each.key]}.cloud-config.yaml"
  content         = module.cloud_init[each.key].user_data
  file_permission = "0644"
}

resource "terraform_data" "node" {
  for_each = var.nodes

  input = {
    instance  = local.instances[each.key]
    data_disk = "${local.instances[each.key]}-data"
    scripts   = abspath(local.scripts)
  }
  # "rke2": a fresh VM per distribution (the K3s VMs are replaced, not converted).
  triggers_replace = [sha256(local.lima_yaml[each.key]), each.value.data_disk, "rke2"]

  provisioner "local-exec" {
    command = "'${abspath(local.scripts)}/node-up.sh' ${self.input.instance} '${abspath(local_file.lima_yaml[each.key].filename)}' ${self.input.data_disk} ${each.value.data_disk}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "'${self.input.scripts}/node-down.sh' ${self.input.instance} ${self.input.data_disk}"
  }
}

resource "terraform_data" "rke2_server" {
  triggers_replace = [terraform_data.node["server"].id, sha256(module.cloud_init["server"].user_data), local.rke2_script_sha]

  provisioner "local-exec" {
    command = "'${abspath(local.scripts)}/rke2-node.sh' server ${local.instances.server} '${abspath(local_file.cloud_init["server"].filename)}' ${var.api_host_port}"
  }
}

resource "terraform_data" "rke2_agent" {
  triggers_replace = [terraform_data.node["agent"].id, terraform_data.rke2_server.id, sha256(module.cloud_init["agent"].user_data), local.rke2_script_sha]

  provisioner "local-exec" {
    command = "'${abspath(local.scripts)}/rke2-node.sh' agent ${local.instances.agent} '${abspath(local_file.cloud_init["agent"].filename)}' ${local.instances.server}"
  }
}

resource "terraform_data" "kubeconfig" {
  input = {
    context = var.kube_context
    scripts = abspath(local.scripts)
  }
  triggers_replace = [terraform_data.rke2_server.id, var.api_host_port]

  provisioner "local-exec" {
    command = "'${self.input.scripts}/kubeconfig.sh' add ${local.instances.server} ${self.input.context} ${var.api_host_port}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "'${self.input.scripts}/kubeconfig.sh' remove ${self.input.context}"
  }
}
