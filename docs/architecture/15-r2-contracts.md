# 15. 스펙 r2 계약: 이벤트 · 통제 시험 결과 · 의존성 인벤토리, 그리고 승인 2종

스펙 r2 11.1절 "11/3 착수 전 고정할 계약" 세 가지와, 같은 날 더한 승인 2종(§8.2 REHEARSE 행, T10, T31)의 경로를 적습니다. 모든 형식은 2026-11-02 21:4x KST의 코드에서 옮겼습니다. 코드가 바뀌면 이 문서를 같이 고칩니다.

| 계약 | 만드는 쪽 → 쓰는 쪽 | 코드 |
|---|---|---|
| 1. SSE 이벤트 payload | T10 → T21 콘솔, T24 감사 | `platform/api/src/bravo_api/events.py`, `statemachine.py`, `changes.py`, `service.py`, `rehearsal.py`, `shift.py` |
| 2. 통제 시험 결과 · 증거 | T5 · T14 → T19 게이트, T21 콘솔 | `platform/gate/src/bravo_gate/controls.py`, `result.py`, `service.py` |
| 3. 의존성 인벤토리 출력 | T16 → T21, ANALYZE, 리허설 | `platform/collector/src/bravo_collector/inventory/detector.py`, `rules.py` |
| 4. 콘솔 화면용 API(승인 2종, 첫 화면) | T10 → T21 | `platform/api/src/bravo_api/rest.py` |
| 5. T31 반영 경로 · Executor 승인 확인 | T10 ↔ T31 `platform/apply`, T12 Executor | 마이그레이션 `0004_change_approval.sql` |
| 6. 리허설 Trial 요청 · 저장소 스냅숏 | T10 → T15 `platform/rehearsal` | `platform/api/src/bravo_api/rehearsal.py`, `upstreams.py` |

## 1. SSE 이벤트

`GET /v1/plans/{id}/events`(SSE, `Last-Event-ID` 또는 `?after=`로 이어 받기). 브라우저는 `POST /v1/plans/{id}/events/ticket`으로 받은 60초 표를 `?ticket=`에 붙입니다. MCP `plan_events`도 같은 객체를 돌려줍니다.

봉투(모든 종류 같음):

```json
{"id": "17", "plan_id": "<uuid>", "state": "REHEARSE", "kind": "change.stage",
 "at": "2026-11-02T12:40:01.123+00:00", "mode": "live", "run_id": null,
 "actor": {"kind": "system", "principal": null}, "data": {}}
```

- `id`: 계획마다 1부터 늘어나는 정수의 문자열. 계획 행 잠금 아래에서 매기므로 commit 순서 = `id` 순서.
- `state`: 이벤트를 쓴 시점의 계획 상태(전이 이벤트는 바뀐 뒤 상태).
- `mode`: `live`만 씁니다. `recorded`는 스키마에만 있고 쓰는 코드가 없습니다(감사 31).
- `actor.kind`: `console` · `mcp` · `system`. `principal`은 사람의 이메일(표시용), 시스템이면 `null`.

`kind`별 `data`:

| kind | data |
|---|---|
| `state.changed` | `from`, `to`, `trigger`, `reason`, 그리고 전이마다 더하는 값: `collect`(COLLECT → ANALYZE), `gaps` · `inventory`(ANALYZE →), `verdict_id` · `purpose`(게이트), `rehearsal_id`, `approval_id` · `epoch` · `stage`(실행 시작), `change_id` · `deployed_digest`(REHEARSE → GATE) |
| `analysis.done` | `inventory` {`status`(ok · unconfigured · error), `reason`, `schema_version`, `rules_version`, `input_digest`, `files_scanned`, `dependencies`, `by_grade`}, `gaps[]` {`id`, `kind`, `grade`, `service`, `file`, `line`, `rule_id`}, `control_differences[]`, `analyzed_at` |
| `gate.verdict` | `verdict_id`, `verdict`(PASS · BLOCK), `reasons[]`, `controls[]` {`control_id`, `src`, `dst`, `verdict`, `reason`}, `readiness` {`lag_bytes`, `sub_state`, `schema_hash`, `seq`, `rows`, `max_lag_bytes`}, `input_digest`, `rule_version`, `purpose`(`gate` · `control_retest` · `change_recheck`) |
| `approval.requested` | `stage`(change · cutover · failback), `approval_request_id`, `expires_at`, `revision`. cutover: `gate_verdict_id`, `artifact_digest`, `applied_image_digest`. failback: `bound_run_id`. change: `change_id`, `artifact_digest`, `diff_digest`, `image_digest`, `policy_diff_digest` |
| `approval.granted` | `approval_id`, `approval_request_id`, `stage`, `revision`, `gate_verdict_id`, `artifact_digest`, `bound_run_id`, `change_id`, `diff_digest`, `image_digest`, `policy_diff_digest`, `expires_at`(해당 없는 키는 `null`) |
| `trial.started` | `rehearsal_id`, `attempt`, `task`, `role`, `status` |
| `trial.proposed` | `rehearsal_id`, `trial_id`, `artifact_digest` |
| `tool.denied` | `rehearsal_id`, `trial_id`, `seq`, `tool`, `rule`, `at` |
| `trial.verdict` | `rehearsal_id`, `trial_id`, `attempt`, `verdict`(PASS · FAIL · INFRA_ERROR), `reason`, `artifact_digest`, `verifier` |
| `change.requested` | `change_id`, `rehearsal_id`, `role`, `plan_revision`, `artifact_digest`, `diff_digest`, `policy_diff_digest`, `policy_targets[]` {`type`, `name`} |
| `change.stage` | `change_id`, `seq`, `phase`(prepare · apply), `name`, `started_at`, `ms`, `result`(OK · FAIL · REFUSED · NOT_CONNECTED · SKIPPED), `detail`(비밀 없음) |
| `change.prepared` | `change_id`, `image_repository`, `image_digest`, `sbom_digest`, `scan` {`scanner`, `version`, `db`, `counts`, `policy`}, `build`, `diff_digest`, `policy_diff_digest`, `policy_targets`, `artifact_digest` |
| `change.applied` | `change_id`, `approval_id`, `approved_image_digest`, `deployed_digest`, `policies_applied`, `gate_recheck`, `signature` |
| `change.prepare_failed` · `change.refused` · `change.failed` | `change_id`, `status`, `reason` |
| `run.stage` | `seq`, `name`, `ms`, `result`, `started_at`(봉투 `run_id`) |
| `run.done` | `status`(DONE · ABORT_PRE_WRITE · SAFE_HOLD), `write_pause_ms` |
| `receipt.saved` | `receipt_id`, `verdict`, `body`(Executor 영수증 그대로 + 콘솔 요약 키 `rows` · `pk_hash` · `content_hash` · `seq` · `summary_of`) |

S2 장면의 순서(시험 `tests/test_changes.py`로 고정): `trial.verdict` PASS → `state.changed`(REHEARSE → REHEARSE, `change_requested`) → `change.requested` → `change.stage`* → `change.prepared` → `approval.requested`(change) → `approval.granted`(change) → `change.stage`* → `change.applied` → `state.changed`(REHEARSE → GATE) → `gate.verdict`(`change_recheck`) → `state.changed`(GATE → AWAIT_APPROVAL) → `approval.requested`(cutover).

## 2. 통제 시험 결과 · 증거(`platform/gate`, :8084)

**판정 입력 행**(`CONTRACT.md` 6절, Executor `/v1/controls`와 같은 형식). `GET /v1/controls?source=<env>&target=<env>[&level=question|control]`:

```json
[{"control_id": "OBJ-01", "question": "Can an anonymous user read the attachment canary object?", "src": "PASS", "dst": "UNKNOWN"}]
```

**전체 결과와 증거.** `GET /v1/controls/report?source=<env>&target=<env>`:

| 필드 | 뜻 |
|---|---|
| `rule_version` | `gate-controls/2026-11-02.1` |
| `source`, `target` | 환경 ID |
| `gate_verdict`, `blocked_controls[]` | PASS · BLOCK, BLOCK인 통제 ID |
| `questions[]` | 질문마다: `control_id`(질문 ID), `parent_control`, `question`, `src`, `dst`, `src_status`, `dst_status`(충족 · 보완 필요 · 미검증), `verdict`, `reason`(`regression` · `fail_both` · `unknown` · `not_equivalent`), `src_evidence`, `dst_evidence` |
| `controls[]` | 통제마다(SEC-01, ENC-01, AUD-01, NET-01, IAM-01, DR-01): `control_id`, `aliases`, `name_ko`, `question`, `src`, `dst`, `src_status`, `dst_status`, `verdict`, `reason`, `questions[]` |
| `input_digest` | 관찰 결과(시각 제외)의 sha256. 같은 입력 → 같은 값 |
| `test_version`, `collected_at`, `expires_at` | 시험기 판, 수집 시각, 만료 시각(`BRAVO_EVIDENCE_TTL_S`, 기본 3600초). 만료된 증거로는 승인하지 않음 |

증거 객체(`src_evidence`, `dst_evidence`): `{"verdict": "PASS|FAIL|UNKNOWN", "reason": "<코드>", "evidence": {...}}`. `evidence`에는 비밀이 없습니다(오류 문자열도 가림, 감사 38). 여러 하위 관찰은 `{"parts": [...]}`. 절이 없는 쪽은 `UNKNOWN` · `not_configured`로 남고 게이트를 막습니다(감사 B16).

## 3. 의존성 인벤토리(`platform/collector`, :8083)

`GET /v1/inventory`(`BRAVO_INVENTORY_ROOT`가 없으면 503). 형식 `inventory/v1`:

| 필드 | 뜻 |
|---|---|
| `schema_version`, `rules_version` | `inventory/v1`, 규칙표 판 |
| `root` | 디렉터리 이름만 |
| `input_digest` | 읽은 파일(상대 경로 + sha256)의 sha256(64 hex). 리허설 스냅숏이 같은 값이어야 함 |
| `files_scanned`, `files_skipped` | 개수 |
| `summary` | 등급 코드별 의존성 수 |
| `findings[]`, `unclassified[]` | 항목: `id`(16 hex), `rule_id`, `tier`(한국어 등급), `tier_code`(`config_swap` · `adapter` · `code_change` · `no_change` · `unclassified`), `service`, `kind`, `file`, `line`, `evidence`(비밀 가림, 200자), `reason`, `hardcoded`, `confidence`(`rule`) |
| `test_summary`, `test_findings[]` | 시험 코드의 항목(이전 범위 밖, 버리지 않음) |

API는 `findings[]`와 `unclassified[]`에서 `tier_code`가 `code_change` · `unclassified`인 항목을 공백으로 셉니다. 공백이 있으면 PROPOSE, 없으면 GATE입니다. 수집기가 설정되지 않았거나 읽지 못하면 ANALYZE에 머물고 `state_reason`이 `collector_unconfigured` 또는 `collector_*`입니다(감사 A4). `GET /v1/clusters`는 환경별 실시간 클러스터 보기이고 `GET /v1/environments`의 `cluster`가 이 값을 씁니다.

## 4. 콘솔 화면용 API

인증: Cloudflare Access JWT(`Cf-Access-Jwt-Assertion`). `/mcp`는 MCP AUD, 나머지는 콘솔 AUD. Access가 설정되지 않으면 503 `access_not_connected`("연결 안 됨: Cloudflare Access"). 모르는 신원은 403 `unknown_principal` + `detail {sub, email}`. 쓰기 요청에는 `Idempotency-Key`(8–200자)가 필요합니다.

**첫 로그인 역할(JIT).** `BRAVO_ACCESS_MEMBERS_FILE`(JSON, 코드 아님)이 이메일 → 역할을 정합니다. 그 신원의 첫 로그인에서 한 번만 멤버십을 만들고, 목록 밖 이메일에는 주지 않습니다. 한 사람에게 operator와 approver를 같이 주지 않습니다. 파일: `deploy/platform/access/members.json`(테넌트 `b-free`).

### 첫 화면

| 요청 | 응답 |
|---|---|
| `GET /v1/plans?limit=100` (viewer 이상) | `{"plans": [{"plan_id", "workload", "state", "state_reason", "rehearsal", "revision", "source": {"environment_id", "name"}, "target": {...}, "pending_approval": {"stage", "approval_request_id", "expires_at", "approve_url"} \| null, "change": {"change_id", "status"} \| null, "created_at", "updated_at", "console_url"}]}` 최신순 |
| `GET /v1/environments` (viewer 이상) | `{"environments": [{"environment_id", "name", "role", "engine", "secret_ref", "secret_status"("resolved" · "unverified"), "secret_reason", "secret_checked_at", "cluster", "created_at"}], "cluster_source": {"status"("ok" · "unconfigured" · "error"), "reason", "observed_at"}}`. `cluster`는 수집기 값 `{"status", "provider", "context", "server_version", "nodes", "error"}`, 또는 `{"status": "not_configured" \| "unknown", "reason"}` |
| `POST /v1/environments` (operator) `{"name", "role": "source"\|"target", "secret_ref"}` | 201 `{"environment_id", "name", "role", "engine", "secret_status", "secret_reason"}`. `secret_ref`는 `tenants/<tenant>/…` 또는 `envs/<이 환경 이름>/…`(플랫폼 OpenBao 배치). 422 `secret_ref_out_of_scope` · `secret_ref_not_found`, 409 `environment_name_taken` |
| `POST /v1/plans` (operator) `{"workload", "source_environment_id", "target_environment_id"}` | 201 `{"plan_id", "state", "state_reason", "revision", "rehearsal", "collect", "analysis", "console_url"}`. MCP `plan_create`와 같은 처리기 |

비밀 참조가 OpenBao에서 풀리지 않은 환경(`unverified`)의 계획은 COLLECT에 머물고 `state_reason`이 `environment_secret_unverified:<이유>`입니다(감사 B11). 이유: `openbao_not_connected`, `openbao_unreachable`, `openbao_denied`, `openbao_credential_unavailable:*`, `openbao_http_<code>`.

실제 환경 등록: `python -m bravo_api.admin register-envs --file deploy/platform/access/environments.json --created-by <operator 이메일>`(owner DSN `BRAVO_MIGRATE_DATABASE_URL`, OpenBao `BRAVO_BAO_*`). 각 `secret_ref`를 OpenBao 메타데이터로 확인하고, 안 풀리면 그 환경은 등록하지 않습니다. 파일의 참조는 2026-11-02 `keys.sh env-list`로 본 실제 경로입니다: `envs/source-aws/chat-db`, `envs/target-onprem/chat-db`, `envs/target-gcp/chat-db`.

### 계획 화면과 승인 2종

`GET /v1/plans/{id}`에 더한 키:

| 키 | 뜻 |
|---|---|
| `change` | 진행 중이거나 마지막 변경. `change_id`, `status`(requested · preparing · prepared · prepare_failed · approved · applying · applied · refused · failed · superseded), `reason`, `role`, `rehearsal_id`, `plan_revision`, `artifact_digest`, `diff_digest`, `policy_diff_digest`, `policy_targets`, `image_repository`, `image_digest`, `sbom_digest`, `scan`, `build`, `signature`, `deployed_digest`, `policies_applied`, `gate_recheck`, `approval_request_id`, `approval_id`, 시각들, `active` |
| `awaiting_approval` | `stage`, `approval_request_id`, `revision`, `gate_verdict_id`(cutover), `artifact_digest`, `applied_image_digest`(cutover), `bound_run_id`(failback), `change`(change: `change_id`, `role`, `artifact_digest`, `diff_digest`, `image_repository`, `image_digest`, `policy_diff_digest`, `policy_targets`, `sbom_digest`, `scan`), `expires_at`, `approve_url` |
| `applied_image_digest` | 목적지에 반영된 승인 digest |
| `security_holds`, `security_holds_reason` | `null`, `not_implemented:D-20`(빈 배열을 "보류 없음"으로 읽지 않도록, 감사 26) |

| 요청 | 응답 |
|---|---|
| `GET /v1/plans/{id}/changes` | `{"plan_id", "changes": [change, …]}` 최신순 |
| `GET /v1/plans/{id}/changes/{change_id}` | change + `stages[]` {`seq`, `phase`, `name`, `started_at`, `ms`, `result`, `detail`} |
| `POST /v1/plans/{id}/changes` (콘솔 operator) | 앞 변경이 prepare_failed · refused · failed로 끝난 뒤 같은 리허설 결과로 새 변경을 엶. 201 `{"plan_id", "change_id", "status": "requested", "diff_digest", "policy_diff_digest"}`. 409 `change_active` · `state_mismatch`, `rehearsal_artifact_missing` |
| `POST /v1/plans/{id}/approvals` (콘솔 approver 전용, MCP 없음) | 아래 |

변경 승인 요청 본문:

```json
{"stage": "change", "approval_request_id": "<awaiting_approval.approval_request_id>",
 "revision": 3, "change_id": "<awaiting_approval.change.change_id>"}
```

201 응답:

```json
{"approval_id": "<uuid>", "plan_id": "<uuid>", "stage": "change", "plan_revision": 3,
 "gate_verdict_id": null, "artifact_digest": "sha256:…", "bound_run_id": null,
 "change_id": "<uuid>", "diff_digest": "sha256:…", "image_digest": "sha256:…",
 "policy_diff_digest": null, "approver": "<세션 주체 이메일>", "expires_at": "…"}
```

- 승인자는 콘솔 세션의 주체입니다. 본문의 `approver`는 무시합니다.
- 결속: 계획 revision, 1회용 요청 ID, 변경 행(`prepared`), 리허설 산출물 digest, diff digest, 이미지 digest, 정책 diff digest. DB 트리거(`bravo_approvals_bind`)가 다시 확인합니다.
- 직무 분리: 승인자 ≠ 변경 요청자(리허설을 시작한 operator) ≠ 계획 작성자. 같으면 403 `separation_of_duties`.
- 오류: 403 `insufficient_scope`(approver 아님 · 콘솔 아님), 409 `approval_stale`(`detail.cause`: `change_mismatch` · `change_not_prepared` · `approval_request_expired`), 409 `state_mismatch`, 409 `approval_request_used`, 400 `idempotency_key_required`, 409 `idempotency_retry`.
- 승인하면 변경이 `approved`가 되고 T31 반영이 시작됩니다. 반영이 승인 digest로 끝나면 시스템이 GATE로 옮기고 게이트를 다시 돌립니다. PASS면 컷오버 승인 요청이 생깁니다. 그 요청자는 변경 요청자이므로 그 사람은 컷오버도 승인하지 못합니다.
- 컷오버 승인 본문은 그대로 `{"stage": "cutover", "approval_request_id", "revision"[, "gate_verdict_id"]}`, 되돌리기는 `"stage": "failback"`.

## 5. T31 반영 경로와 Executor의 승인 확인

**플랫폼 DB 계약(마이그레이션 0004).** T31 워커(`platform/apply`, 역할 `bravo_apply`)는 `changes`, `change_stages`만 씁니다. 계획 상태는 API만 바꿉니다.

| 상태 | 누가 | 다음 |
|---|---|---|
| `requested` | API(리허설 PASS) | 워커가 `preparing`으로 가져감 |
| `preparing` → `prepared` · `prepare_failed` | 워커: diff 적용 → buildx → syft → trivy → `image_digest`, `sbom_digest`, `scan`, `build` | API가 변경 승인 요청 |
| `prepared` → `approved` | API(콘솔 승인, 같은 트랜잭션) | 워커가 `applying`으로 가져감 |
| `applying` → `applied` · `refused` · `failed` | 워커: 승인 확인 → 서명(OpenBao transit) → 목적지 반입 → digest 일치 배포 → 정책 → 게이트 재대조 | API가 GATE로(applied) 또는 REHEARSE에 이유와 함께 머묾 |

- DB 트리거 `bravo_changes_guard`: 역할마다 허용 전이만, 결속 값은 바꿀 수 없음, 준비 뒤 후보 digest는 고정, `approved`는 같은 변경 · 같은 digest의 `change` 승인 행이 있을 때만.
- 제약 `changes_applied_digest`: `applied`이면 `deployed_digest = image_digest`.
- 워커의 승인 확인(`platform/apply/src/bravo_apply/approvals.py`): 승인은 플랫폼 DB의 `approvals` 행에서만 읽습니다. 파일 · 인자로 받은 승인은 없습니다. 거부 이유: `approval_source_unavailable`, `approval_missing`, `approval_not_found`, `approval_stage_mismatch`, `approval_change_mismatch`, `approval_not_granted`, `approver_is_requester`, `approval_expired`. 2026-11-02 온프렘에서 `approval None`으로 배포된 경로(CLI 바인딩 파일)는 이렇게 막았습니다.
- 워커 실행: `python -m bravo_apply worker`(`BRAVO_APPLY_DATABASE_URL`), CLI `apply`는 `--approval-id`와 같은 DB가 필요. `BRAVO_APPLY_GATE_URL`이 없으면 `gate.recheck`는 `NOT_CONNECTED`(`gate_unconfigured`). 단계 이름과 이유 코드는 `platform/apply/README.md`.

**Executor 승인 확인(감사 A2).** Executor는 자기 DB 역할 `bravo_executor`로 `approvals`를 읽습니다(0004: `GRANT SELECT ON approvals`, RLS 정책 `executor_cell`, 읽기만). 서명된 승인 토큰은 만들지 않았습니다.

| 열 | 뜻 |
|---|---|
| `approval_id` uuid | 실행 요청의 `approval_id` |
| `plan_id` uuid | 실행 요청의 계획과 같아야 함 |
| `stage` text | `change` · `cutover` · `failback`. 실행 종류와 같아야 함(`change`는 Executor 실행을 시작할 수 없음) |
| `approver_id`, `requester_id`, `proposer_id` uuid | 승인자 ≠ 요청자 ≠ 계획 작성자(DB 트리거가 기록 때 강제) |
| `expires_at` timestamptz | 지나면 거부 |
| `consumed_at`, `consumed_by`, `run_idem_key` | API가 실행을 시작할 때 소비, `run_idem_key` = `<stage>-<approval_id>`(변경 승인은 `change-<change_id>`) |
| `plan_revision`, `gate_verdict_id`, `artifact_digest`, `bound_run_id`, `change_id`, `diff_digest`, `image_digest`, `policy_diff_digest` | 결속 값 |

## 6. 리허설 Trial 요청과 저장소 스냅숏

API → `platform/rehearsal`. 순서: 수집기 인벤토리 → 수집기 스냅숏(헤더가 인벤토리 `input_digest`와 같아야 함) → `PUT /v1/snapshots/{tenant_id}/{plan_id}` → `POST /v1/trials`.

```json
{"plan_id": "<uuid>", "tenant_id": "<tenant>", "rehearsal_id": "<uuid>",
 "attempt": 1, "max_attempts": 3, "task": "fix-secrets-manager", "role": "app_migration", "state": "PROPOSE",
 "prior_failures": [{"attempt": 1, "verdict": "FAIL", "reason": "<code>", "summary": "attempt 1: verifier verdict FAIL (<reason>)"}],
 "evidence": [{"id": "gate:<verdict_id>:<control>", "kind": "gate_control", "summary": "…"},
              {"id": "inv:<finding id>", "kind": "inventory_finding", "summary": "<tier> <service> <kind> <file>:<line> (<rule_id>)"}],
 "source_env": "<source 환경 이름>", "target_env": "<target 환경 이름>",
 "analysis": {…}, "inventory_digest": "<64 hex>"}
```

- 헤더: `Authorization: Bearer <BRAVO_REHEARSAL_API_TOKEN>`, `Idempotency-Key: rehearsal-<rehearsal_id>`.
- `attempt` = 지금까지의 FAIL 판정 수 + 1. 전제 조건 · 인프라 재시도는 같은 번호를 씁니다.
- 스냅숏: `PUT` 본문 tar.gz, 헤더 `X-Bravo-Inventory-Digest: <input_digest>`, `Content-Type: application/gzip`. 200 `{tenant_id, plan_id, input_digest, files, withheld}`, 422 `{"error": "snapshot_digest_mismatch" | …}`.
- **연결 안 됨:** 수집기에 스냅숏 엔드포인트가 아직 없습니다. 제안: `GET /v1/inventory/snapshot`(스캔한 파일만 tar.gz, 헤더 `X-Bravo-Inventory-Digest`, 루트가 없으면 503). 그전까지 실제 수집기로는 모든 리허설 시작이 `snapshot_unavailable`로 멈추고, 시도로 세지 않습니다.
- 시도로 세지 않는 "아무것도 돌지 않음" 이유: `llm_unconfigured`, `cluster_unconfigured`, `rehearsal_unreachable`, `task_unknown`, `verifier_unconfigured`, `tool_layer_unconfigured`, `role_task_mismatch`, `role_unknown`, `role_allowlist_missing`, `role_allowlist_invalid`, `unauthorized`, `envelope_invalid`, `state_not_allowed_for_role`, `agent_runtime_unavailable`, `openbao_unconfigured`, `openbao_unreachable`, `openbao_denied`, `llm_token_missing`, `bad_request`, `idempotency_key_reuse`, `inventory_unavailable`, `collector_unreachable`, `collector_response_lost`, `collector_bad_response`, `snapshot_unavailable`, `snapshot_inventory_mismatch`, `snapshot_upload_failed`, 그리고 리허설 서비스의 스냅숏 거부 코드(`snapshot_*`, `bad_inventory_digest`, `bad_snapshot_key`, `inventory_digest_missing`).

## 스펙 밖

`GET /v1/plans`, `GET /v1/environments`, `POST /v1/plans/{id}/changes`, JIT 역할 파일, `envs/<이름>/…` 비밀 참조는 스펙 §8.9에 없습니다. 2026-11-02 코디네이터 지시(초보 고객이 첫 화면에서 모든 것을 보고 시작)로 넣었습니다. 회의에서 `docs/spec/DEVIATIONS.md`에 올릴지 정합니다.
