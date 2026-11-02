# 환경마다 Cloudflare Tunnel 하나와 진입 이름 하나, 그 위에 가중치 LB(D-16).
# D-26: 상태에 비밀이 없음. tunnel_secret을 넘기지 않아 Cloudflare가 만들고,
# 연결 토큰은 Terraform이 읽지 않음. 플랫폼 키 단계(deploy/pilot/scripts/keys.sh issue-tunnel-token)가
# API에서 받아 플랫폼 OpenBao에 바로 씀.

data "cloudflare_zone" "this" {
  zone_id = var.zone_id
}

locals {
  zone_name = data.cloudflare_zone.this.name
  hostnames = { for env, _ in var.environments : env => "${var.app}-${env}.${local.zone_name}" }
  # LB에 넣을 환경. 비우면 전부. 요금제의 포함 끝점 수(지금 2개)를 넘지 않게 고름.
  lb_env_names = var.lb_environments == null ? keys(var.environments) : var.lb_environments
  lb_envs      = var.lb_enabled ? { for env, e in var.environments : env => e if contains(local.lb_env_names, env) } : {}
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "env" {
  for_each   = var.environments
  account_id = var.account_id
  name       = "bravo-${var.app}-${each.key}"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "env" {
  for_each   = var.environments
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.env[each.key].id
  config = {
    ingress = [
      {
        hostname = local.hostnames[each.key]
        service  = each.value.origin_service
      },
      # LB 원본은 터널 주소로 들어오므로 Host가 LB 이름일 때도 같은 대상으로 보냄
      {
        hostname = "${var.app}.${local.zone_name}"
        service  = each.value.origin_service
      },
      { service = "http_status:404" },
    ]
  }
}

resource "cloudflare_dns_record" "env" {
  for_each = var.environments
  zone_id  = var.zone_id
  name     = local.hostnames[each.key]
  type     = "CNAME"
  content  = "${cloudflare_zero_trust_tunnel_cloudflared.env[each.key].id}.cfargotunnel.com"
  proxied  = true
  ttl      = 1
  comment  = "bravo ${var.app} ${each.key} tunnel"
}

# 대표 이름을 활성 환경 터널에 바로 연결. LB를 켜도 지우지 않음: LB가 같은 이름보다 우선하고,
# LB를 끄면 이 레코드가 다시 답하므로 비상 경로가 됨(15-multicloud-traffic.md E1).
resource "cloudflare_dns_record" "front" {
  count   = 1
  zone_id = var.zone_id
  name    = "${var.app}.${local.zone_name}"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.env[var.active_env].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "bravo ${var.app} front (active env)"

  lifecycle {
    # 컷오버 · 되돌리기 때 Executor가 대상 터널을 바꿈. Terraform이 되돌리지 않게 함.
    ignore_changes = [content]
  }
}

resource "cloudflare_load_balancer_monitor" "app" {
  count          = var.lb_enabled ? 1 : 0
  account_id     = var.account_id
  type           = "https"
  method         = "GET"
  path           = var.health_path
  expected_codes = "200"
  # The pool origins no longer override Host (below), so the monitor names a host the tunnels route: the LB name,
  # which every app tunnel's ingress and every chat HTTPRoute serve (Cloudflare tunnel-behind-LB setup).
  header      = { Host = ["${var.app}.${local.zone_name}"] }
  interval    = 60
  timeout     = 5
  retries     = 2
  description = "bravo ${var.app} health"
}

resource "cloudflare_load_balancer_pool" "env" {
  for_each   = local.lb_envs
  account_id = var.account_id
  name       = "bravo-${var.app}-${each.key}"
  monitor    = cloudflare_load_balancer_monitor.app[0].id
  origins = [{
    name    = each.key
    address = "${cloudflare_zero_trust_tunnel_cloudflared.env[each.key].id}.cfargotunnel.com"
    enabled = true
    # No Host override (2026-11-03): requests keep Host = the LB name, which each tunnel's ingress and HTTPRoute
    # accept, so a plain-HTTP 301 points back to b-free-chat instead of the environment name the LB picked.
  }]
}

resource "cloudflare_load_balancer" "app" {
  count           = var.lb_enabled ? 1 : 0
  zone_id         = var.zone_id
  name            = "${var.app}.${local.zone_name}"
  proxied         = true
  steering_policy = "random"
  default_pools   = [for env, _ in local.lb_envs : cloudflare_load_balancer_pool.env[env].id]
  # 모든 풀이 나쁠 때 갈 곳은 활성 환경(15-multicloud-traffic.md E2). keys()는 이름순이라 dst가 될 수 있었음.
  fallback_pool = cloudflare_load_balancer_pool.env[var.active_env].id
  random_steering = {
    default_weight = 0
    pool_weights   = { for env, e in local.lb_envs : cloudflare_load_balancer_pool.env[env].id => e.weight }
  }

  lifecycle {
    # 이전 중 가중치(0 → 10 → 50 → 100%)는 콘솔 승인 뒤 Executor가 API로 바꿈. Terraform이 되돌리지 않게 함.
    ignore_changes = [random_steering, fallback_pool]
  }
}
