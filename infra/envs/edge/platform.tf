# 플랫폼(우리 서비스) 진입: b-free.<zone> → 플랫폼 EKS의 Gateway. 고객 앱은 b-free-chat*(module.edge).
# 류지환 지시(2026-11-02): "b-free에는 고객 앱이 아니라 우리 플랫폼 서비스를 띄우는거야."
# D-26: tunnel_secret을 넘기지 않음. 연결 토큰은 keys.sh issue-tunnel-token platform <id>로 OpenBao에만.
# 인증: 플랫폼 API는 Cloudflare Access JWT만 받음. Zero Trust를 켜기 전에는 로그인할 수 없음(공개 우회 없음).

variable "platform_hostname" {
  type    = string
  default = "b-free"
}

variable "platform_origin_service" {
  description = "플랫폼 클러스터 안 진입점(같은 계약: Gateway bravo-gateway, ns bravo-edge)"
  type        = string
  default     = "http://cilium-gateway-bravo-gateway.bravo-edge.svc.cluster.local:80"
}

data "cloudflare_zone" "platform" {
  zone_id = var.zone_id
}

locals {
  platform_fqdn = "${var.platform_hostname}.${data.cloudflare_zone.platform.name}"
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "platform" {
  account_id = var.account_id
  name       = "bravo-platform"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "platform" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.platform.id
  config = {
    ingress = concat(
      [{ hostname = local.platform_fqdn, service = var.platform_origin_service }],
      local.platform_link_ingress, # platform_links.tf (D-16 links the platform exposes)
      [{ service = "http_status:404" }],
    )
  }
}

resource "cloudflare_dns_record" "platform" {
  zone_id = var.zone_id
  name    = local.platform_fqdn
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.platform.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "bravo platform (console · API · MCP)"
}

output "platform_hostname" {
  value = local.platform_fqdn
}

output "platform_tunnel_id" {
  value = cloudflare_zero_trust_tunnel_cloudflared.platform.id
}
