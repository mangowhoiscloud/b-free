# D-16 환경 사이 링크(제안, 02-network.md 4절). 테넌트 하나 · 노출 환경 하나 단위.
# 비밀이 아닌 것만 만듦: 관리 Tunnel, 링크마다 Access 앱(self_hosted) + Service Auth 정책, 프록시 CNAME.
# 만들지 않는 것(D-26): Tunnel 토큰과 서비스 토큰. 플랫폼 키 단계가 Cloudflare API에서 받아 OpenBao에만 씀.
#
# Tunnel 구성은 원격(Cloudflare) 구성이 아니라 클러스터 안 설정 파일(config_src = "local", 02-network.md 4.2).
# 그래서 Cloudflare 쪽 권한만으로 라우트를 더해 새 서비스를 내놓을 수 없음. 설정 파일 내용은 출력
# connector_ingress로 내고, deploy/platform/link/customer-mgmt-tunnel/ 의 ConfigMap이 같은 값을 가짐.
#
# 순서: Access 앱과 정책을 먼저 만들고 DNS 레코드(라우트)를 나중에 만듦. 지울 때는 반대(depends_on).
# 그래서 앱 없는 라우트가 생기지 않음.

data "cloudflare_zone" "this" {
  zone_id = var.zone_id
}

locals {
  zone_name   = data.cloudflare_zone.this.name
  tunnel_name = "${var.tenant}-${var.environment_short}-mgmt"

  hostnames = { for k, _ in var.links : k => "${var.tenant}-${var.environment_short}-${k}.${local.zone_name}" }

  # 토큰 이름 <client>-<노출 환경>-<서비스>(02-network.md 4.1), OpenBao 경로는 소비 환경 쪽.
  client_tokens = {
    for k, l in var.links : k => [
      for c in l.clients : {
        name         = "${c}-${var.environment_short}-${k}"
        openbao_path = "secret/envs/${var.client_environment_ids[c]}/cf-link-${var.environment_short}-${k}"
      }
    ]
  }

  active_links = var.enabled ? var.links : {}
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "mgmt" {
  count = var.enabled ? 1 : 0

  account_id = var.account_id
  name       = local.tunnel_name
  config_src = "local"
}

resource "cloudflare_zero_trust_access_policy" "link" {
  for_each = local.active_links

  account_id = var.account_id
  name       = "${var.tenant}-${var.environment_short}-${each.key} service auth"
  # Service Auth. Allow 정책은 서비스 토큰을 인증하지 않음(Cloudflare 서비스 토큰 문서, 02-network.md 4.2).
  decision = "non_identity"
  include  = [for id in lookup(var.service_token_ids, each.key, []) : { service_token = { token_id = id } }]

  lifecycle {
    precondition {
      condition     = length(lookup(var.service_token_ids, each.key, [])) > 0
      error_message = "링크 ${each.key}에 서비스 토큰 ID가 없습니다. keys.sh로 토큰을 만든 뒤 ID를 넘기세요."
    }
  }
}

resource "cloudflare_zero_trust_access_application" "link" {
  for_each = local.active_links

  account_id           = var.account_id
  name                 = "${var.tenant}-${var.environment_short}-${each.key}"
  type                 = "self_hosted"
  domain               = local.hostnames[each.key]
  session_duration     = var.session_duration
  app_launcher_visible = false

  policies = [{
    id         = cloudflare_zero_trust_access_policy.link[each.key].id
    precedence = 1
  }]
}

resource "cloudflare_dns_record" "link" {
  for_each = local.active_links

  zone_id = var.zone_id
  name    = local.hostnames[each.key]
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.mgmt[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "bravo D-16 link ${var.tenant} ${var.environment_short} ${each.key}"

  # 앱 · 정책 뒤에 만들고 앞에 지움.
  depends_on = [
    cloudflare_zero_trust_access_application.link,
    cloudflare_zero_trust_access_policy.link,
  ]
}
