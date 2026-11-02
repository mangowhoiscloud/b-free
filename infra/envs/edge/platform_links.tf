# Links that the PLATFORM exposes to customer clusters (D-16, D-26): consumers run `cloudflared access tcp` with their
# own Access service token. Today:
#   bao  (t1-plat-bao -> platform OpenBao): GKE target External Secrets (gke-plat-bao), on-prem RGW SSE-KMS (dst-plat-bao,
#        2026-11-03: RGW wraps object data keys with transit key rgw-target-onprem; client deploy/landing-zone/overlays/
#        rke2/rook/rgw-kms-link.yaml, Service rook-ceph/openbao, only RGW and its token renewer may use it).
#   otel (t1-plat-otel -> platform Collector): OTel agents of GKE, source EKS and on-prem.
# Same pattern as edge_link (Access app + Service Auth policy first, DNS last), but the connector is
# the platform tunnel bravo-platform (platform.tf), so the ingress rules join its remote config.
# Token secrets: keys.sh issue-service-token writes them to OpenBao only (secret/envs/<consumer>/cf-link-plat-<svc>).
# Token IDs (not secret) go to terraform.tfvars platform_links.<key>.token_ids. Service tokens last 168h: a new token
# has a new ID, so rotation = issue, add the ID, apply, re-pipe the Secret, remove the old ID, apply.

variable "platform_links" {
  description = "링크 키(예: bao, otel) → 원본 tcp:// 서비스와 이 링크를 쓸 서비스 토큰 ID 목록. 호스트 이름 t1-plat-<키>.<zone>"
  type = map(object({
    service   = string
    token_ids = list(string)
  }))
  default = {}
}

locals {
  platform_links = var.zero_trust_enabled ? var.platform_links : {}
  platform_link_hosts = {
    for k, _ in local.platform_links : k => "t1-plat-${k}.${data.cloudflare_zone.platform.name}"
  }
  platform_link_ingress = [
    for k, l in local.platform_links : { hostname = local.platform_link_hosts[k], service = l.service }
  ]
}

resource "cloudflare_zero_trust_access_policy" "platform_link" {
  for_each = local.platform_links

  account_id = var.account_id
  name       = "t1-plat-${each.key} service auth"
  decision   = "non_identity"
  include    = [for id in each.value.token_ids : { service_token = { token_id = id } }]

  lifecycle {
    precondition {
      condition     = length(each.value.token_ids) > 0
      error_message = "플랫폼 링크 ${each.key}에 서비스 토큰 ID가 없습니다."
    }
  }
}

resource "cloudflare_zero_trust_access_application" "platform_link" {
  for_each = local.platform_links

  account_id           = var.account_id
  name                 = "t1-plat-${each.key}"
  type                 = "self_hosted"
  domain               = local.platform_link_hosts[each.key]
  session_duration     = "24h"
  app_launcher_visible = false

  policies = [{
    id         = cloudflare_zero_trust_access_policy.platform_link[each.key].id
    precedence = 1
  }]
}

resource "cloudflare_dns_record" "platform_link" {
  for_each = local.platform_links

  zone_id = var.zone_id
  name    = local.platform_link_hosts[each.key]
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.platform.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "bravo platform link ${each.key} (Access service auth)"

  depends_on = [cloudflare_zero_trust_access_application.platform_link]
}

output "platform_link_hosts" {
  value = local.platform_link_hosts
}
