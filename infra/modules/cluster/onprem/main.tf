# On-prem destination cluster (D-21): K3s on two Lima VMs (server + agent) on this laptop.
# Lima has no Terraform provider, so each step is a terraform_data with a local-exec provisioner;
# apply creates, destroy deletes (NFR-7). No secret enters the state: the K3s join token moves
# between the VMs through a pipe only (scripts/k3s-node.sh).

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
  k3s_files_sha = sha256(join("", [
    for f in ["psa.yaml", "audit-policy.yaml", "sysctl-k3s.conf", "k3s.service"] : filesha256("${path.module}/files/${f}")
  ]))
  k3s_script_sha = filesha256("${local.scripts}/k3s-node.sh")
}

resource "local_file" "lima_yaml" {
  for_each        = local.lima_yaml
  filename        = "${local.lima_dir}/${local.instances[each.key]}.yaml"
  content         = each.value
  file_permission = "0644"
}

resource "terraform_data" "node" {
  for_each = var.nodes

  input = {
    instance  = local.instances[each.key]
    data_disk = "${local.instances[each.key]}-data"
    scripts   = abspath(local.scripts)
  }
  triggers_replace = [sha256(local.lima_yaml[each.key]), each.value.data_disk]

  provisioner "local-exec" {
    command = "'${abspath(local.scripts)}/node-up.sh' ${self.input.instance} '${abspath(local_file.lima_yaml[each.key].filename)}' ${self.input.data_disk} ${each.value.data_disk}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "'${self.input.scripts}/node-down.sh' ${self.input.instance} ${self.input.data_disk}"
  }
}

resource "terraform_data" "k3s_server" {
  triggers_replace = [terraform_data.node["server"].id, var.k3s.version, var.k3s.sha256, local.k3s_files_sha, local.k3s_script_sha]

  provisioner "local-exec" {
    command = "'${abspath(local.scripts)}/k3s-node.sh' server ${local.instances.server} '${var.k3s.version}' ${var.k3s.sha256} ${var.api_host_port}"
  }
}

resource "terraform_data" "k3s_agent" {
  triggers_replace = [terraform_data.node["agent"].id, terraform_data.k3s_server.id, local.k3s_files_sha, local.k3s_script_sha]

  provisioner "local-exec" {
    command = "'${abspath(local.scripts)}/k3s-node.sh' agent ${local.instances.agent} '${var.k3s.version}' ${var.k3s.sha256} ${local.instances.server}"
  }
}

resource "terraform_data" "kubeconfig" {
  input = {
    context = var.kube_context
    scripts = abspath(local.scripts)
  }
  triggers_replace = [terraform_data.k3s_server.id, var.api_host_port]

  provisioner "local-exec" {
    command = "'${self.input.scripts}/kubeconfig.sh' add ${local.instances.server} ${self.input.context} ${var.api_host_port}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "'${self.input.scripts}/kubeconfig.sh' remove ${self.input.context}"
  }
}
