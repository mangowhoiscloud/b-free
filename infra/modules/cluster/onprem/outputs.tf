output "instances" {
  description = "Lima instance names (limactl shell <name>)."
  value       = local.instances
}

output "kube_context" {
  description = "kubectl context for this cluster (merged into ~/.kube/config)."
  value       = var.kube_context
  depends_on  = [terraform_data.kubeconfig, terraform_data.rke2_agent]
}

output "api_server" {
  description = "RKE2 API as seen from the laptop (Lima port forward)."
  value       = "https://127.0.0.1:${var.api_host_port}"
}
