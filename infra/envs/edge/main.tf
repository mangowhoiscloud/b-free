module "edge" {
  source       = "../../modules/edge/cloudflare"
  account_id   = var.account_id
  zone_id      = var.zone_id
  app          = var.app
  environments = var.environments
  lb_enabled   = var.lb_enabled
  active_env   = var.active_env
}

output "tunnel_ids" {
  value = module.edge.tunnel_ids
}

output "hostnames" {
  value = module.edge.hostnames
}

output "front_hostname" {
  value = module.edge.front_hostname
}

output "pool_ids" {
  value = module.edge.pool_ids
}
