# 비밀 값 없음(D-26). 터널 연결 토큰은 플랫폼 키 단계가 OpenBao에만 씀.
output "tunnel_ids" {
  value = { for env, t in cloudflare_zero_trust_tunnel_cloudflared.env : env => t.id }
}

output "hostnames" {
  value = local.hostnames
}

output "front_hostname" {
  value = "${var.app}.${local.zone_name}"
}

output "pool_ids" {
  value = { for env, p in cloudflare_load_balancer_pool.env : env => p.id }
}
