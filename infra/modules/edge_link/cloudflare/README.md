# edge_link/cloudflare

D-16(제안) 환경 사이 링크를 Cloudflare에 만드는 모듈입니다. 테넌트 하나, 서비스를 내놓는 환경 하나 단위입니다. 설계는 `docs/architecture/02-network.md` 4절입니다.

## 게이트

`enabled = false`(기본)면 아무 자원도 만들지 않습니다. Cloudflare Zero Trust(Access)를 사용자가 켠 뒤에만 `true`로 바꿉니다. Access 앱 없이 링크 호스트를 내놓지 않기 위해서입니다.

## 만드는 것(비밀이 아닌 것만)

| 자원 | 이름 | 비고 |
|---|---|---|
| 관리 Tunnel | `<tenant>-<env>-mgmt` | `config_src = "local"`. 라우트는 클러스터 안 설정 파일(출력 `connector_ingress`) |
| Access 앱 | `<tenant>-<env>-<link>` | `self_hosted`, 링크 호스트 하나 |
| Access 정책 | `<tenant>-<env>-<link> service auth` | `decision = "non_identity"`(Service Auth). `service_token_ids`의 토큰만 |
| DNS 레코드 | `<tenant>-<env>-<link>.<zone>` | 프록시 CNAME → `<tunnel>.cfargotunnel.com`. 앱 · 정책 뒤에 만들고 앞에 지움 |

## 만들지 않는 것

Tunnel 토큰과 서비스 토큰(Client ID · Secret)입니다. 플랫폼 키 단계(`deploy/pilot/scripts/keys.sh`)가 Cloudflare API에서 받아 플랫폼 OpenBao에만 씁니다(D-26). 그래서 상태 파일에 비밀이 없습니다. 이 모듈은 토큰 ID(비밀 아님)만 입력으로 받습니다.

## 입력

| 변수 | 뜻 |
|---|---|
| `enabled` | Zero Trust 게이트 |
| `tenant`, `environment_id`, `environment_short` | 예: `t1`, `source-aws`, `src` |
| `links` | 링크 키 → `{ service = "tcp://이름:포트", clients = ["plat", "dst"] }` |
| `service_token_ids` | 링크 키 → 서비스 토큰 ID 목록. `enabled = true`면 모든 링크에 하나 이상 |

## 출력

| 출력 | 뜻 |
|---|---|
| `hostnames` | 링크 키 → 호스트 이름 |
| `tunnel_id` | 관리 Tunnel ID(꺼져 있으면 null) |
| `access_aud_tags` | 링크 키 → Access 앱 AUD. 커넥터 `originRequest.access` 검사에 씀 |
| `connector_ingress` | 노출 환경 cloudflared 설정 파일의 ingress |
| `openbao_paths` | 커넥터 토큰 `secret/envs/<env>/cf-mgmt-tunnel`, 클라이언트 토큰 `secret/envs/<소비 환경>/cf-link-<env>-<link>` |

## 확인하지 못한 것

- `tcp://` 원본에 앱 없는 라우트가 닿지 않는지(N3 (e)), 정책 없는 앱에 닿지 않는지(N3)는 Zero Trust를 켠 뒤 시험합니다.
- 커넥터의 `originRequest.access` 검사는 팀 이름이 Zero Trust를 켠 뒤에 정해져서 아직 설정 파일에 넣지 않았습니다.
