# 플랫폼(b-free.<zone>) 앞의 Cloudflare Access. 콘솔 · API는 호스트 전체 앱, 운영자 MCP는 /mcp 경로 앱.
# 두 앱의 AUD가 달라 콘솔 세션으로 MCP를 부르거나 그 반대가 되지 않음(docs/architecture/08-operator-mcp.md 5절).
# 허용은 platform_access_emails의 사람만(One-time PIN 로그인). 이메일은 gitignore된 terraform.tfvars에만 둠.
#
# 게이트: Zero Trust(Access)를 계정에서 켠 뒤 platform_access_enabled = true.
# 켜기 전에는 아무것도 만들지 않음. 그동안 b-free에 cloudflared 커넥터를 띄우지 않음(공개 노출 방지).
#
# AUD와 팀 도메인은 비밀이 아님. 플랫폼 API · operator-mcp가 Cf-Access-Jwt-Assertion을 검증할 때
# iss = access_team_domain, aud = 그 호스트의 AUD로 씀(CLOUD-E2E-CONTRACT.md 6절).

variable "platform_access_enabled" {
  description = "Zero Trust(Access)를 켠 뒤 true. 플랫폼 Access 앱 두 개와 정책을 만듦"
  type        = bool
  default     = false
}

variable "access_team_name" {
  description = "Zero Trust 팀 이름(<team>.cloudflareaccess.com의 <team>). 온보딩 때 정한 값"
  type        = string
  default     = ""
}

variable "platform_access_emails" {
  description = "플랫폼(콘솔 · API · MCP)에 들어올 수 있는 사람의 이메일. terraform.tfvars(gitignore)에만 둠"
  type        = list(string)
  default     = []
}

variable "platform_access_allowed_idps" {
  description = "허용할 로그인 방법 ID(One-time PIN IdP ID). 비우면 계정의 모든 로그인 방법"
  type        = list(string)
  default     = []
}

locals {
  platform_access    = var.platform_access_enabled ? 1 : 0
  access_team_domain = var.access_team_name == "" ? null : "https://${var.access_team_name}.cloudflareaccess.com"
}

resource "cloudflare_zero_trust_access_policy" "platform_operators" {
  count      = local.platform_access
  account_id = var.account_id
  name       = "bravo platform operators"
  decision   = "allow"
  include    = [for e in var.platform_access_emails : { email = { email = e } }]

  lifecycle {
    precondition {
      condition     = length(var.platform_access_emails) > 0 && var.access_team_name != ""
      error_message = "platform_access_emails와 access_team_name을 terraform.tfvars에 넣으세요."
    }
  }
}

# 콘솔 · API(호스트 전체). 브라우저는 302로 팀 도메인 로그인에 감.
resource "cloudflare_zero_trust_access_application" "platform" {
  count                      = local.platform_access
  account_id                 = var.account_id
  name                       = "bravo-platform"
  type                       = "self_hosted"
  domain                     = local.platform_fqdn
  session_duration           = "24h"
  app_launcher_visible       = false
  allowed_idps               = length(var.platform_access_allowed_idps) > 0 ? var.platform_access_allowed_idps : null
  auto_redirect_to_identity  = length(var.platform_access_allowed_idps) == 1
  http_only_cookie_attribute = true
  same_site_cookie_attribute = "lax"

  policies = [{
    id         = cloudflare_zero_trust_access_policy.platform_operators[0].id
    precedence = 1
  }]
}

# 운영자 MCP(/mcp). Access Managed OAuth가 인가 서버(RFC 8414 · 9728 메타데이터, DCR, RFC 8707).
# 토큰 없는 MCP 클라이언트는 302가 아니라 401 + WWW-Authenticate를 받음.
# 경로가 더 구체적인 앱이 호스트 앱보다 우선함.
resource "cloudflare_zero_trust_access_application" "platform_mcp" {
  count                     = local.platform_access
  account_id                = var.account_id
  name                      = "bravo-platform-mcp"
  type                      = "self_hosted"
  domain                    = "${local.platform_fqdn}/mcp"
  session_duration          = "24h"
  app_launcher_visible      = false
  allowed_idps              = length(var.platform_access_allowed_idps) > 0 ? var.platform_access_allowed_idps : null
  auto_redirect_to_identity = length(var.platform_access_allowed_idps) == 1

  oauth_configuration = {
    enabled = true
    dynamic_client_registration = {
      enabled = true
      # Claude Code 등 CLI 클라이언트는 localhost 콜백을 씀
      allow_any_on_localhost = true
      allow_any_on_loopback  = true
    }
    grant = {
      access_token_lifetime = "15m"  # 08 문서 7절: 15분 불투명 토큰, refresh 때 정책 재평가
      session_duration      = "168h" # 해커톤 기간(11/9까지)
    }
  }

  policies = [{
    id         = cloudflare_zero_trust_access_policy.platform_operators[0].id
    precedence = 1
  }]
}

output "access_team_domain" {
  description = "Cf-Access-Jwt-Assertion의 iss. 공개 키는 <이 값>/cdn-cgi/access/certs"
  value       = local.access_team_domain
}

output "platform_access_aud" {
  description = "b-free.<zone>(콘솔 · API) Access 앱의 AUD. 비밀 아님"
  value       = one(cloudflare_zero_trust_access_application.platform[*].aud)
}

output "platform_mcp_access_aud" {
  description = "b-free.<zone>/mcp(운영자 MCP) Access 앱의 AUD. 비밀 아님"
  value       = one(cloudflare_zero_trust_access_application.platform_mcp[*].aud)
}
