output "enabled" {
  description = "자원을 만들었는지(Zero Trust 게이트)"
  value       = var.enabled
}

output "hostnames" {
  description = "링크 키 → 호스트 이름. enabled = false면 이름만 있고 DNS 레코드는 없음"
  value       = local.hostnames
}

output "tunnel_id" {
  description = "관리 Tunnel ID. enabled = false면 null"
  value       = var.enabled ? cloudflare_zero_trust_tunnel_cloudflared.mgmt[0].id : null
}

output "tunnel_name" {
  description = "관리 Tunnel 이름"
  value       = local.tunnel_name
}

output "access_aud_tags" {
  description = "링크 키 → Access 앱 AUD(커넥터 originRequest.access 검사에 씀). enabled = false면 빈 맵"
  value       = { for k, a in cloudflare_zero_trust_access_application.link : k => a.aud }
}

output "connector_ingress" {
  description = "노출 환경의 cloudflared 설정 파일 ingress(config_src = local). 마지막 규칙은 http_status:404"
  value = concat(
    [for k, l in var.links : { hostname = local.hostnames[k], service = l.service }],
    [{ hostname = null, service = "http_status:404" }],
  )
}

output "openbao_paths" {
  description = "토큰이 사는 플랫폼 OpenBao 경로(KV v2, 값은 Terraform 밖). 커넥터 토큰과 링크별 클라이언트 토큰"
  value = {
    connector_token = "secret/envs/${var.environment_id}/cf-mgmt-tunnel"
    client_tokens   = local.client_tokens
  }
}
