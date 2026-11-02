# platform/api

플랫폼 API, 상태 머신, SSE 이벤트 스트림입니다(T10). 같은 폴더의 `edge/`는 T17, `audit/`는 T24가 주인입니다(`platform/README.md`).

## 계약 초안(§8.9). 11/3에 고정

| 엔드포인트 | 입력 → 출력 |
|---|---|
| `POST /v1/environments` | 환경 종류, 접속 정보 참조(`tenants/<tenant_id>/…` 아래만) → `environment_id` |
| `POST /v1/plans` | workload, source, target → `plan_id`, 의존성 목록, 통제 차이 |
| `POST /v1/plans/{id}/rehearsals` | `task`, `role`(governance, app_migration, ci) → `rehearsal_id`, 시도 번호, `trial_id`, 상태(`requested`, `running`, `INFRA_ERROR`+이유). 판정은 리허설 서비스의 검증기가 내고, API는 결속을 확인한 결과만 받음(아래 "리허설") |
| `POST /v1/plans/{id}/gate` | → 질문별 원본 · 대상 · 판정 |
| `POST /v1/plans/{id}/approvals` | 콘솔 전용. `stage`(`change`, `cutover`, `failback`), 1회용 `approval_request_id`, 화면에 보인 `revision`, `change`이면 `change_id`. 결속 값(plan, diff, digest, 대상, 만료)은 그 revision이 고정함 → 결속된 승인. 승인자는 입력이 아니고 콘솔 세션의 주체임 |
| `POST /v1/plans/{id}/shift` | 콘솔 전용. `cutover`(AWAIT_APPROVAL) 또는 `failback`(WATCH, Executor `POST /v1/failback`) → 실행 handle(`run_id`), 단계 결과, 쓰기 정지 ms, 무결성 영수증 |
| `GET /v1/environments/{id}/controls` | → 통제별 시험 결과, 증거, 수집 시각 |
| `GET /v1/plans/{id}/events` | SSE. `Last-Event-ID`로 재접속 |
| `GET`, `POST /v1/plans/{id}/exceptions`, `POST …/exceptions/{exception_id}/approve`, `…/reject` | 통제 예외(스펙 S1 5b). 콘솔 operator가 요청, 콘솔 approver가 결정(요청자 · 계획 작성자와 다른 사람). 마지막 게이트 판정의 측정된 FAIL 행 · 목적지 · 증거 digest에 결속. UNKNOWN은 걸 수 없음. 계약 `docs/architecture/15-r2-contracts.md` 9절 |

- 모든 실행 요청에 `Idempotency-Key`. 키의 범위는 (테넌트, 동작, 입력 digest)입니다. 같은 키에 다른 입력이면 409입니다.
- 같은 키에 같은 입력이 다시 오면 처음 만든 실행 handle(`run_id`, `rehearsal_id`)과 그 결과를 돌려줍니다. 새 실행을 만들지 않습니다.
- 각 실행은 자기 `stage`의 유효한 승인이 있어야 시작합니다(아래 "승인과 실행 시작").
- 상태와 전이 조건은 `docs/architecture/03-migration.md` 3절에 있습니다.

## 제안(스펙에 없음)

| 항목 | 제안 |
|---|---|
| 가중치 단계 전환 | `POST /v1/plans/{id}/weights`(D-10). 콘솔 전용 |
| 예외 승인 | D-11은 `approvals`의 `stage=exception`을 제안. 2026-11-02 구현은 별도 경로 `/v1/plans/{id}/exceptions`와 표 `control_exceptions`(마이그레이션 0008, 계약 15 9절) |
| 위치 필드 | `environments`에 `location`. GOV-01 표시용(§2.1). 판정에는 D-03b 채택 뒤에만 씀 |
| 계획 조회 | `GET /v1/plans/{id}`: 상태, 마지막 게이트 판정, 승인 대기와 승인 주소, 걸린 보류, 진행 중 실행, 마지막 영수증 요약. MCP `plan_get`과 콘솔이 같이 씀(D-18) |
| 보안 보류 | `POST /v1/holds`, `GET /v1/holds`, `POST /v1/holds/{id}/release-requests`, `POST /v1/holds/{id}/release`. 생성은 콘솔의 operator 이상, 해제 승인은 콘솔의 `security_approver`. P0에 최소 해제 양식 하나를 둠. 실행 요청은 `security_hold`로 거부될 수 있음(D-20, `docs/architecture/04-governance.md` 5절) |

SSE 이벤트 필드(제안): `id`, `plan_id`, `state`, `kind`, `at`, `mode`(`live`, `recorded`, `mock`), `run_id`, `actor`(`kind`: `console`, `mcp`, `system`), `data`. `kind`별 `data` 형식은 11/3에 표로 고정합니다. 리허설 이벤트 종류는 `trial.started`, `tool.denied`, `trial.verdict`이고, `data`에 `trial_id`를 넣어 콘솔에서 해당 트레이스를 열 수 있게 합니다(`docs/architecture/07-runtime-mesh-observability.md` 6절).

## 승인 2종과 T31(스펙 r2, 2026-11-02)

리허설 PASS는 REHEARSE에 머물고 변경(`changes` 행)을 엽니다. T31 워커(`platform/apply`)가 후보(이미지 digest, SBOM, 스캔)를 만들면 `change` 승인 요청이 생깁니다. 승인은 diff · 이미지 digest · 정책 diff에 결속되고, 반영이 승인 digest로 끝나야 REHEARSE → GATE로 가며 시스템이 게이트를 다시 돌립니다. PASS면 `cutover` 승인 요청입니다. 경로, 본문, 이벤트, 워커 계약, Executor의 승인 확인은 `docs/architecture/15-r2-contracts.md`에 있습니다. 첫 화면용 `GET /v1/plans`, `GET /v1/environments`도 같은 문서에 있습니다.

| 변수 | 쓰는 곳 |
|---|---|
| `BRAVO_ACCESS_TEAM_DOMAIN`(또는 `BRAVO_JWT_ISSUER` + `BRAVO_JWKS_URL`), `BRAVO_CONSOLE_AUD`, `BRAVO_MCP_AUD` | Cloudflare Access. 없으면 503 `access_not_connected` |
| `BRAVO_ACCESS_MEMBERS_FILE` | 첫 로그인 역할 허용 목록(JSON). 예: `deploy/platform/access/members.json` |
| `BRAVO_BAO_ADDR`, `BRAVO_BAO_CACERT`, `BRAVO_BAO_ROLE_ID_FILE` + `BRAVO_BAO_SECRET_ID_FILE`(또는 `BRAVO_BAO_TOKEN_FILE`), `BRAVO_BAO_KV_MOUNT` | `secret_ref` 확인(메타데이터만). 없으면 환경은 `unverified` |
| `BRAVO_CHANGE_POLL_INTERVAL_S` | 변경 감시 주기(기본 1초) |

## 승인과 실행 시작(제안 D-18)

승인과 실행 시작은 콘솔에서 사람이 합니다. 스펙이 트래픽 전환, 컷오버, 되돌리기를 AI가 관여하지 않는 실행 구간으로 두기 때문입니다(§8.2). 근거와 흐름은 `docs/architecture/08-operator-mcp.md` 4절과 6절에 있습니다.

**공통 인가.** 모든 처리기는 먼저 `authorize(principal, action, channel)` 하나를 부릅니다. `channel`은 `console`, `mcp`, `service` 중 하나입니다. REST와 MCP가 같은 처리기를 쓰므로, 라우터의 인증 설정에 기대지 않습니다.

| 동작 | 통과하는 채널과 주체 |
|---|---|
| 승인 결정, 보류 해제 승인 | `console`의 사람 주체, 역할 `approver` 또는 `security_approver` |
| 트래픽 전환, 컷오버, 되돌리기 시작 | `console`의 사람 주체, 역할 `operator` |
| 리허설, 게이트 실행 요청 | `console` 또는 `mcp`의 사람 주체, 역할 `operator` |
| 조회 | 모든 채널의 사람 주체, 역할 `viewer` 이상 |

서비스 신원(다른 Pod의 메시 신원이나 앱 토큰)은 사용자의 권한을 대신 쓰지 못합니다.

**승인 요청과 revision.**
- 상태 머신이 AWAIT_APPROVAL에 들어갈 때 1회용 `approval_request_id`와 불변 revision을 만듭니다. revision은 결속 값(plan, diff, digest, 대상, 만료)의 묶음입니다.
- 내용이 바뀌면 새 revision과 새 승인 요청을 만듭니다. 옛 요청은 무효가 됩니다.

**승인 결정.**
- 경로는 `POST /v1/plans/{id}/approvals` 하나이고 콘솔 전용입니다. 콘솔 Access 앱의 AUD만 받습니다.
- 콘솔 세션, CSRF 토큰, 1회용 `approval_request_id`에 묶습니다. 같은 요청 ID는 한 번만 결정할 수 있습니다.
- 승인자는 콘솔 세션의 주체(`principals.id`)입니다. 요청 본문에서 받지 않습니다.
- 직무 분리는 DB 제약(CHECK 또는 트리거)으로 강제합니다. 승인자는 그 승인 요청의 요청자와 다르고, 승인 대상 revision의 제안자와도 달라야 합니다.
- 이 경로의 Access 세션을 짧게 두어 다시 로그인하게 합니다(제안). 대상 경로는 `console.<도메인>/v1/plans/*/approvals`입니다. 별도 Access 앱이 필요하면 AUD 표에 행이 늘어납니다(확인 필요).

**실행 시작.** `shift`와 `weights`는 아래를 한 트랜잭션에서 하고 한 번만 commit합니다.
1. 계획 행을 `SELECT … FOR UPDATE`로 잠급니다.
2. 현재 revision이 승인의 revision과 같은지 확인합니다.
3. 그 단계의 승인이 있고, 만료 전이고, 아직 소비되지 않았는지 확인합니다.
4. 범위에 걸린 보안 보류가 없는지 확인합니다(D-20).
5. 같은 계획에 진행 중인 실행이 없는지 확인합니다.
6. 승인을 소비하고 실행 행(`run_id`)을 만듭니다.
7. commit합니다. 하나라도 어긋나면 아무것도 쓰지 않고 이유 코드(`approval_stale`, `approval_missing`, `security_hold`, `run_active`)로 거부합니다.

**거부 시험.**

| ID | 시험 | 통과 기준 |
|---|---|---|
| A1 | MCP 신원으로 승인 처리기와 보류 해제 처리기를 직접 부름 | 거부 |
| A2 | 서비스 신원(콘솔 Pod 등)으로 같은 처리기를 부름 | 거부 |
| A3 | 요청 본문에 다른 승인자를 넣음 | 무시하고 세션 주체로 기록 |
| A4 | 승인자가 승인 요청자나 대상 revision의 제안자와 같음 | DB 제약으로 거부 |
| A5 | CSRF 토큰 없이 결정, 쓴 `approval_request_id`로 다시 결정 | 거부 |
| A6 | 승인 뒤 계획 revision이 바뀐 상태로 실행 시작 | `approval_stale` |
| A7 | 같은 승인으로 다른 키의 실행 시작을 동시에 두 번 보냄 | 실행 하나만 생기고 나머지는 거부 |
| A8 | 같은 키와 같은 입력으로 실행 시작을 다시 보냄 | 같은 `run_id`를 돌려줌 |

## 운영자 MCP 서버(D-18)

같은 이미지를 별도 Deployment `operator-mcp`로 띄우고 `/mcp`(MCP 2026-07-28, Streamable HTTP, stateless)만 켭니다. 도구 일곱 개(`env_register`, `env_controls`, `plan_create`, `plan_get`, `plan_events`, `gate_run`, `rehearsal_start`)는 위 엔드포인트의 처리기를 그대로 부릅니다. 승인, 보류 해제, 실행 시작(`shift`, `weights`)은 MCP에 없습니다. `operator_mcp` DB 역할은 승인 · 보류 표에 쓰기 권한이 없고, 실행 요청은 `rehearsal`, `gate` 종류만 INSERT할 수 있습니다. `operator-mcp`는 MCP용 Access 앱의 AUD만, 이 API는 콘솔용 AUD만 받습니다. 주체는 `(iss, sub)`이고 `principals`와 `memberships(principal_id, tenant_id, role)`로 찾습니다. 설계는 `docs/architecture/08-operator-mcp.md` 5절과 9절입니다.

## 상태 경로와 리허설(2026-11-02)

상태: `COLLECT → ANALYZE → (GATE | PROPOSE) ; PROPOSE → REHEARSE → GATE → AWAIT_APPROVAL → CUTOVER → WATCH → (DONE | FAILBACK → DONE_ROLLED_BACK)`, 실패 `ABORT_PRE_WRITE`, `SAFE_HOLD`, `NEEDS_HUMAN`. 전이표는 `src/bravo_api/statemachine.py` 하나입니다. SHIFT(Edge 가중치, FR-8)는 이름만 있고 전이가 없습니다. Edge 제어기 계약이 없기 때문입니다. 구현 선택은 `docs/spec/DEVIATIONS.md` D-25.

**리허설.** `rehearsals` 표가 시도 카운터의 주인입니다. API는 리허설 서비스(`platform/rehearsal`, `BRAVO_REHEARSAL_URL`)에 시도 하나당 Trial 하나를 요청하고(`Idempotency-Key: rehearsal-<id>`), `RehearsalWatcher`가 결과를 읽습니다. 결과는 계획 · 시도 · Trial ID · 산출물 digest(다시 계산) · 검증기 기록(실행 ID, Pod UID, 이미지 digest, task 판, reward)이 맞을 때만 받고, 아니면 `INFRA_ERROR verdict_unbound:*`입니다. 종료 상태는 한 번만 정합니다. FAIL 3회면 NEEDS_HUMAN(FR-5), 인프라 오류는 2회 상한(07 3절). 아무것도 돌지 않은 전제 조건(`llm_unconfigured`, `cluster_unconfigured` 등)은 기록만 하고 세지 않습니다. 컷오버 뒤에는 `rehearsal_not_allowed`(S2-AC5).

**선택 상류(없으면 "설정 안 됨"으로 기록, 값을 만들지 않음).**

| 변수 | 쓰는 곳 |
|---|---|
| `BRAVO_REHEARSAL_URL`, `BRAVO_REHEARSAL_API_TOKEN` | 리허설 서비스. 없으면 `rehearsal_unconfigured`(503) |
| `BRAVO_COLLECTOR_URL` | ANALYZE가 `GET /v1/inventory`를 읽음. 형식은 `upstreams.CollectorClient` 주석(11/3 수집기 계약으로 확정) |
| `BRAVO_GATE_URL`, `BRAVO_GATE_TIMEOUT_S`(기본 60) | 게이트가 `GET /v1/controls/report?source=<출발지 환경 이름>&target=<목적지 환경 이름>`의 질문 행(목적지 증거 digest 포함)과 증거 만료 시각을 더 읽음. 같은 통제 ID가 두 곳에서 오면 UNKNOWN → BLOCK. 게이트가 모르는 환경이면 `gate_controls_unknown_environment` |
