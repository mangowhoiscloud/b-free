output "kube_context" {
  value = module.cluster.kube_context
}

output "api_server" {
  value = module.cluster.api_server
}

output "instances" {
  value = module.cluster.instances
}
