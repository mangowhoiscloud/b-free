# D-16 환경 사이 링크(제안, docs/architecture/02-network.md 4절). 테넌트 t1, 노출 환경 출발지 · 목적지.
# Zero Trust를 켜기 전에는 아무것도 만들지 않음. 그동안의 경로는 임시 링크(D-16 전)
# (deploy/platform/link/temporary/, CLOUD-E2E-CONTRACT.md 7절).
#
# 켜는 순서:
#   1. 사용자가 Cloudflare Zero Trust(Access)를 켬
#   2. keys.sh로 서비스 토큰 9개를 만들어 OpenBao에 씀(값은 OpenBao에만), 토큰 ID를 link_service_token_ids에 넣음
#   3. zero_trust_enabled = true 로 apply
#   4. 커넥터 토큰을 각 환경 cf-edge 에, 클라이언트 토큰을 소비 환경 bravo-link 에 파이프로 전달
#   5. kubectl apply -k deploy/platform/link/d16 , 임시 링크 삭제

variable "zero_trust_enabled" {
  description = "Cloudflare Zero Trust(Access)를 사용자가 켠 뒤 true. 그전에는 아무것도 만들지 않음"
  type        = bool
  default     = false
}

variable "link_service_token_ids" {
  description = "환경 약칭(src, dst) → 링크 키(k8s, db, pgb) → 서비스 토큰 ID 목록. ID는 비밀이 아님"
  type        = map(map(list(string)))
  default     = {}
}

locals {
  link_tenant = "t1"

  # 02-network.md 4.2 표. 원본 이름은 CLOUD-E2E-CONTRACT.md 3절(chat-db-rw, chat-pgbouncer)을 따름.
  # dst-s3(첨부 복사, T8): 온프렘 RGW Service rook-ceph-rgw-bravo(443, TLS). 2026-11-02 22:5x 추가.
  link_exposing_envs = {
    src = {
      environment_id = "source-aws"
      links = {
        k8s = { service = "tcp://kubernetes.default.svc:443", clients = ["plat"] }
        db  = { service = "tcp://chat-db-rw.chat.svc:5432", clients = ["plat", "dst"] }
        pgb = { service = "tcp://chat-pgbouncer.chat.svc:6432", clients = ["plat"] }
        # Write-block switch (spec r2 §8.10, CLOUD-E2E-CONTRACT 8): Executor -> chat-admin:8081 (Bearer token).
        admin = { service = "tcp://chat-admin.chat.svc:8081", clients = ["plat"] }
      }
    }
    dst = {
      environment_id = "target-onprem"
      links = {
        k8s   = { service = "tcp://kubernetes.default.svc:443", clients = ["plat"] }
        db    = { service = "tcp://chat-db-rw.chat.svc:5432", clients = ["plat", "src"] }
        pgb   = { service = "tcp://chat-pgbouncer.chat.svc:6432", clients = ["plat"] }
        admin = { service = "tcp://chat-admin.chat.svc:8081", clients = ["plat"] }
        # Attachment copy S3 -> RGW (T8, D-22): the Executor's object-store endpoint on the destination.
        s3 = { service = "tcp://rook-ceph-rgw-bravo.rook-ceph.svc:443", clients = ["plat"] }
        # RGW's plain listener :80 for the platform gate's OBJ-06 only (a plain request must get AccessDenied from
        # the TLS-only bucket policy). Client: the gate's s3-local sidecar. 2026-11-03 01:0x 추가.
        s3http = { service = "tcp://rook-ceph-rgw-bravo.rook-ceph.svc:80", clients = ["plat"] }
      }
    }
    # GKE 목적지(target-gcp): 플랫폼 게이트의 DB 질문(DB-*, DR-ARCHIVE)용 chat-db 링크. 2026-11-03 01:1x 추가.
    # 커넥터는 GKE cf-edge 의 cloudflared-mgmt(deploy/platform/link/customer-mgmt-tunnel/overlays/target-gcp).
    gke = {
      environment_id = "target-gcp"
      links = {
        db = { service = "tcp://chat-db-rw.chat.svc:5432", clients = ["plat"] }
      }
    }
  }
}

module "edge_link" {
  source   = "../../modules/edge_link/cloudflare"
  for_each = local.link_exposing_envs

  enabled           = var.zero_trust_enabled
  account_id        = var.account_id
  zone_id           = var.zone_id
  tenant            = local.link_tenant
  environment_id    = each.value.environment_id
  environment_short = each.key
  links             = each.value.links
  service_token_ids = lookup(var.link_service_token_ids, each.key, {})
}

output "link_gate" {
  description = "D-16 링크 게이트 상태와 이름. zero_trust_enabled = false면 Cloudflare 자원은 없음"
  value = {
    zero_trust_enabled = var.zero_trust_enabled
    hostnames          = { for e, m in module.edge_link : e => m.hostnames }
    tunnel_ids         = { for e, m in module.edge_link : e => m.tunnel_id }
    openbao_paths      = { for e, m in module.edge_link : e => m.openbao_paths }
  }
}
