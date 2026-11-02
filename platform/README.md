# platform

플랫폼(본부) 서비스입니다. 모두 `platform-aws` 클러스터(Amazon EKS, D-21)에서 돕니다. 경계는 스펙 §7.4의 컴포넌트 표를 따릅니다. 코드 위치와 작업 주인(스펙 §11.2)은 다를 수 있어 따로 적습니다.

| 폴더 | 컴포넌트 | 성격 | LLM | 작업과 주인 |
|---|---|---|---|---|
| `api/` | 콘솔 · API, 상태 머신, 이벤트 스트림 | 결정적 | 금지 | T10 데이터 · 컷오버 |
| `api/edge/` | Edge 가중치 제어 | 결정적 | 금지 | T17 콘솔 · 관측 · Edge |
| `api/audit/` | 감사 로그 | 결정적 | 금지 | T24 보안 통제 · 게이트 |
| `collector/` | 읽기 전용 수집기 | 결정적 | 금지 | 새 작업 T30 제안(D-05) |
| `agent/` | 에이전트 워커, 도구 계층, 의존성 인벤토리 | 비결정적 | 사용 | T11, T16 에이전트 · 리허설 |
| `rehearsal/` | 리허설 러너, Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 어댑터 | 결정적 틀 안에서 AI가 시도 | 에이전트만 | T3, T15, T26 에이전트 · 리허설 |
| `gate/` | 통제 시험기, 원본 대조 게이트 | 결정적 | 금지 | T5, T14, T19 보안 통제 · 게이트 |
| `governance/` | 규제 매핑표(D-03a) | 결정적 | 금지 | T23 보안 통제 · 게이트 |
| `executor/` | Cutover Executor, 무결성 영수증, 되돌리기, Exit Drill | 결정적 | 금지 | T12, T13, T20, T27 데이터 · 컷오버 |
| `console/` | 운영 콘솔 | 결정적 | 금지 | T21 콘솔 · 관측 · Edge |

## 네임스페이스

| 네임스페이스 | 면 | 무엇 |
|---|---|---|
| `bravo-control` | 제어면 | 위 표의 결정적 서비스, 운영자 MCP 서버(`operator-mcp`, D-18), 리허설 서비스(Harbor 오케스트레이터 + 도구 계층), LLM 게이트웨이, 플랫폼 DB. Executor Pod는 메시에서 제외. Executor는 StatefulSet replica 1(제안 D-19) |
| `bravo-agents` | 실행면 | Trial마다 뜨는 에이전트 Pod. 클러스터 · 클라우드 · DB 자격 없음. Trial 토큰 하나만 |
| `bravo-observe` | 관측 | 관측 백엔드(Collector 포함 올인원 1개) |
| `bravo-link`(제안 D-16) | 연결 | 상대 환경의 K8s API, DB, PgBouncer를 로컬 Service 이름으로 받는 링크 Pod. 메시 밖. 허용한 호출자 Pod만 받음 |

메시와 관측 구성은 `docs/architecture/07-runtime-mesh-observability.md`에 있습니다.

## 경계 규칙

- "LLM 금지" 폴더는 LLM 클라이언트(`anthropic`, `claude_agent_sdk` 등)를 import하지 않습니다(NFR-5). import 검사 스크립트로 강제할 계획입니다.
- 폴더 사이는 계약(HTTP API나 문서로 고정한 형식)으로만 잇습니다. 다른 폴더의 내부 모듈을 import하지 않습니다.
- 클러스터 자격은 호출 주체별로 나눕니다(`docs/architecture/02-network.md` 3절). 리허설은 프로비저너 계정과 exec 계정이 따로입니다. `agent/`는 어떤 클러스터 자격도 갖지 않습니다.

## 플랫폼 비밀

플랫폼에는 가장 강한 자격이 모입니다. 어디에 두고 어떻게 바꾸는지 정합니다(제안).

| 비밀 | 쓰는 곳 | 보관 | 회전 · 만료 |
|---|---|---|---|
| AWS 자격 | 수집기(읽기), 시험기, 배포. Executor에는 주지 않음(P0, 제안 D-19) | 워크로드마다 IAM 역할을 따로 둠. EKS Pod Identity로 서비스 계정마다 역할을 연결(Pod Identity Agent는 관리형 add-on). 노드 역할은 Pod가 쓰지 못하게 IMDSv2 hop 제한 1 | 역할 세션이 자동 만료 |
| 클러스터 계정 토큰(3절의 주체별) | 수집기, 시험기, 리허설, Executor, 배포 | AWS Secrets Manager → ESO → 플랫폼 K8s Secret | 데모 기간 짧은 만료, 끝나면 폐기. 제안 D-16: 출발지 EKS는 IAM 역할 단기 토큰(access entry)이라 저장하지 않음, 온프렘 K3s는 서비스 계정 토큰 168시간(`docs/architecture/02-network.md` O12) |
| DB 제어 역할, PgBouncer 관리 사용자 | Executor | 같음 | 데모 뒤 폐기 |
| 관찰 전용 자격: DB 역할 `executor_observe`(카탈로그 · 상태 조회만), PgBouncer `stats_users`(SHOW만) | Executor 관찰 전용 모드(제안 D-19) | 같음. 제어 자격과 다른 Secret | 데모 뒤 폐기 |
| Cloudflare LB 범위 토큰 | Edge 제어 | 같음 | 발표 뒤 폐기 |
| Cloudflare 서비스 토큰(`plat-*`, 링크마다 하나) | `bravo-link` 링크 Pod. Secret을 파일로 마운트해 읽음(환경 변수 아님) | 같음. 경로는 `tenants/<tenant_id>/…`(제안 D-16). 부트스트랩 스크립트가 Secrets Manager에 바로 씀 | 수명 7일. 11/6 회전 연습(회전하면 링크 Pod 재시작). 11/7–11/8 회전 없음. 11/9 폐기 |
| LLM API 키 | LLM 게이트웨이 | 같음. 에이전트 프로세스에는 넣지 않음 | 발표 뒤 폐기 |

**콘솔 로그인.** Cloudflare Access를 콘솔 Tunnel 앞에 둡니다(제안). 콘솔은 MCP와 다른 Access 앱입니다. 운영팀과 승인자만 들어옵니다. API는 `Cf-Access-Jwt-Assertion`을 고정 issuer, RS256, 필수 클레임, 콘솔 앱 AUD로 검사합니다. 주체는 `(iss, sub)`로 정합니다. 주체, 역할, 테넌트는 `SECURITY DEFINER` 함수 `auth_lookup(iss, sub)`로만 찾습니다. 이 함수가 `principals`와 `memberships(principal_id, tenant_id, role)`를 읽습니다. 그다음 트랜잭션마다 `SET LOCAL app.tenant_id`를 합니다(`docs/architecture/02-network.md` 8.2절). 이메일은 표시용입니다. 승인 기록의 승인자는 콘솔 세션의 주체이고, 요청 본문에서 받지 않습니다(`docs/architecture/08-operator-mcp.md` 5절).

**플랫폼 밖에 두는 Cloudflare 자격(제안 D-16).** Tunnel, Access 앱, 토큰을 만드는 Cloudflare API 토큰은 `edge_link` Terraform 파이프라인과 토큰 부트스트랩 스크립트에만 둡니다(`infra/README.md`). 고객 클러스터에는 자기 관리 Tunnel 토큰과 자기 링크 토큰만 둡니다. 그래서 한 클러스터가 털려도 새 Tunnel이나 Access 앱을 만들 수 없습니다.
