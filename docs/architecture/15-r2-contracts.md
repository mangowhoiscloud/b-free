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
| 7. 관측 API(콘솔 관측 화면) | 플랫폼 관측 백엔드(T18) → T21 콘솔 | `deploy/platform/observability`, `deploy/landing-zone/base/otel-agent-values.yaml`, `deploy/landing-zone/observability` |
| 8. 비용 영수증 API(마무리 장면, NFR-8) | T1 수집기 → T10 API → T21 콘솔 | `platform/collector/src/bravo_collector/costing/`, `platform/api/src/bravo_api/service.py`(`cost`), `deploy/platform/control/config/cost.yaml` |
| 9. 통제 예외(S1 5b) | T10 API ↔ T21 콘솔, T19 게이트 | `platform/api/src/bravo_api/exceptions.py`, `gate.py`, 마이그레이션 `0008_control_exceptions.sql`, `platform/gate/src/bravo_gate/controls.py` |

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
| `questions[]` | 질문마다: `control_id`(질문 ID), `parent_control`, `question`, `src`, `dst`, `src_status`, `dst_status`(충족 · 보완 필요 · 미검증), `verdict`, `reason`(`regression` · `fail_both` · `unknown` · `not_equivalent`), `src_evidence`, `dst_evidence`, `src_evidence_digest`, `dst_evidence_digest`(9.5) |
| `controls[]` | 통제마다(SEC-01, ENC-01, AUD-01, NET-01, IAM-01, DR-01): `control_id`, `aliases`, `name_ko`, `question`, `src`, `dst`, `src_status`, `dst_status`, `verdict`, `reason`, `questions[]` |
| `input_digest` | 관찰 결과(시각 제외)의 sha256. 같은 입력 → 같은 값 |
| `test_version`, `collected_at`, `expires_at` | 시험기 판, 수집 시각, 만료 시각(`BRAVO_EVIDENCE_TTL_S`, 기본 3600초). 만료된 증거로는 승인하지 않음 |

증거 객체(`src_evidence`, `dst_evidence`): `{"verdict": "PASS|FAIL|UNKNOWN", "reason": "<코드>", "evidence": {...}}`. `evidence`에는 비밀이 없습니다(오류 문자열도 가림, 감사 38). 여러 하위 관찰은 `{"parts": [...]}`. 절이 없는 쪽은 `UNKNOWN` · `not_configured`로 남고 게이트를 막습니다(감사 B16).

## 3. 의존성 인벤토리(`platform/collector`, :8083)

`GET /v1/inventory`(`BRAVO_INVENTORY_ROOT`가 없으면 503). 스캔할 파일이 하나도 없으면(빈 emptyDir, 실패한 checkout) 503 `inventory_root_empty`입니다. "의존성 없음"이 아닙니다. 형식 `inventory/v1`:

| 필드 | 뜻 |
|---|---|
| `schema_version`, `rules_version` | `inventory/v1`, 규칙표 판 |
| `root` | 디렉터리 이름만 |
| `input_digest` | 읽은 파일(상대 경로 + sha256)의 sha256(64 hex). 리허설 스냅숏이 같은 값이어야 함 |
| `files_scanned`, `files_skipped` | 개수 |
| `summary` | 등급 코드별 의존성 수 |
| `findings[]`, `unclassified[]` | 항목: `id`(16 hex), `rule_id`, `tier`(한국어 등급), `tier_code`(`config_swap` · `adapter` · `code_change` · `no_change` · `unclassified`), `service`, `kind`, `file`, `line`, `evidence`(비밀 가림, 200자), `reason`, `hardcoded`, `confidence`(`rule`) |
| `test_summary`, `test_findings[]` | 시험 코드의 항목(이전 범위 밖, 버리지 않음) |

API는 `inventory_root_empty`를 "인벤토리 없음"으로 받습니다. 계획은 ANALYZE에 머물고 `state_reason`과 `plan.analysis.inventory`가 `{"status": "unavailable", "reason": "inventory_root_empty"}`입니다. 수집기 장애(`collector_*`)와 구분하고, 리허설 시작에서는 시도로 세지 않습니다.

API는 `findings[]`와 `unclassified[]`에서 `tier_code`가 `code_change` · `unclassified`인 항목을 공백으로 셉니다. 공백이 있으면 PROPOSE, 없으면 GATE입니다. 수집기가 설정되지 않았거나 읽지 못하면 ANALYZE에 머물고 `state_reason`이 `collector_unconfigured` 또는 `collector_*`입니다(감사 A4). `GET /v1/clusters`는 환경별 실시간 클러스터 보기이고 `GET /v1/environments`의 `cluster`가 이 값을 씁니다.

## 4. 콘솔 화면용 API

인증: Cloudflare Access JWT(`Cf-Access-Jwt-Assertion`). `/mcp`는 MCP AUD, 나머지는 콘솔 AUD. Access가 설정되지 않으면 503 `access_not_connected`("연결 안 됨: Cloudflare Access"). 모르는 신원은 403 `unknown_principal` + `detail {sub, email}`. 쓰기 요청에는 `Idempotency-Key`(8–200자)가 필요합니다.

**첫 로그인 역할(JIT).** `BRAVO_ACCESS_MEMBERS_FILE`(JSON, 코드 아님)이 이메일 → 역할을 정합니다. 그 신원의 첫 로그인에서 한 번만 멤버십을 만들고, 목록 밖 이메일에는 주지 않습니다. 한 사람에게 operator와 approver를 같이 주지 않습니다. 파일: `deploy/platform/access/members.json`(테넌트 `b-free`).

### 첫 화면

| 요청 | 응답 |
|---|---|
| `GET /v1/me` (멤버 누구나) | `{"email", "tenant_id", "roles": [...], "role": "<가장 높은 역할>", "principal": {"id", "iss", "sub"}}` |
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
- 수집기 `GET /v1/inventory/snapshot`(2026-11-02 저녁 추가): 스캔한 파일만 tar.gz, 헤더 `X-Bravo-Inventory-Digest`. 파일이 없으면 503 `{"error": "inventory_root_empty"}`. 실제 수집기 → 실제 리허설 서비스까지 시험이 통과합니다(`tests/test_rehearsal.py::test_real_collector_snapshot_reaches_the_real_rehearsal_service`).
- 시도로 세지 않는 "아무것도 돌지 않음" 이유: `llm_unconfigured`, `cluster_unconfigured`, `rehearsal_unreachable`, `task_unknown`, `verifier_unconfigured`, `tool_layer_unconfigured`, `role_task_mismatch`, `role_unknown`, `role_allowlist_missing`, `role_allowlist_invalid`, `unauthorized`, `envelope_invalid`, `state_not_allowed_for_role`, `agent_runtime_unavailable`, `openbao_unconfigured`, `openbao_unreachable`, `openbao_denied`, `llm_token_missing`, `bad_request`, `idempotency_key_reuse`, `inventory_unavailable`, `inventory_root_empty`, `collector_unreachable`, `collector_response_lost`, `collector_bad_response`, `snapshot_unavailable`, `snapshot_inventory_mismatch`, `snapshot_upload_failed`, 그리고 리허설 서비스의 스냅숏 거부 코드(`snapshot_*`, `bad_inventory_digest`, `bad_snapshot_key`, `inventory_digest_missing`).

## 7. 관측 API(콘솔 관측 화면)

콘솔 관측 화면은 플랫폼 Prometheus를 콘솔 nginx의 읽기 전용 프록시로 질의합니다. 새 서비스 코드는 없고, 값은 모두 각 환경에서 실제로 받은 OTLP와 Hubble 지표입니다. 2026-11-02 22:10 KST에 아래 질의를 실제로 돌린 결과를 `deploy/platform/observability/runs/console-promql-20261102T131033Z.jsonl`에 남겼습니다.

### 7.1 경로

| 출처 | 경로 | 상태(2026-11-02 22:10 KST) |
|---|---|---|
| 플랫폼 서비스(api, operator-mcp, executor 등) | OTLP를 `otel-collector.bravo-observe.svc:4317`로 바로 보냄 | 연결됨 |
| 플랫폼 서비스 상태 | 플랫폼 Collector의 `http_check` 수신기가 15초마다 각 서비스의 상태 경로를 GET | 연결됨(8개) |
| 출발지 EKS(`source-aws`) | 앱 → `otel-agent.bravo-observability` → `platform-otel`(cloudflared `access tcp`, 서비스 토큰 `src-plat-otel`) → `t1-plat-otel.mangowhois.ai` → 터널 `bravo-platform` → 플랫폼 Collector | 연결됨 |
| 온프렘 RKE2(`target-onprem`) | 같은 경로, 서비스 토큰 `dst-plat-otel` | 연결됨 |
| GKE(`target-gcp`) | 같은 경로, 서비스 토큰 `gke-plat-otel`(`deploy/platform/link/gke-consumer`, 플랫폼 갈래) | 연결됨 |

- 링크 Access 앱 `t1-plat-otel`의 Service Auth 정책은 토큰 셋만 받습니다(`infra/envs/edge/terraform.tfvars`의 `platform_links.otel.token_ids`). 토큰 값은 OpenBao `secret/envs/<env>/cf-link-plat-otel`에만 있고, 설치 스크립트 `deploy/landing-zone/scripts/otel-link.sh`가 파이프로 Secret을 만듭니다. 토큰 만료는 2026-11-09 13:02 UTC(168시간)입니다.
- 환경 에이전트는 모든 신호에 `bravo.env`를 붙입니다. 플랫폼 Collector는 이 값이 없을 때만 `platform`을 넣습니다. Prometheus 라벨 이름은 `bravo_env`입니다.
- 노트북 프로세스는 이 경로에 없습니다. GKE의 옛 임시 링크(`links.sh`의 `platform-otel` 중계)는 플랫폼 갈래가 D-16 링크로 바꿨습니다.

### 7.2 엔드포인트

| 요청 | 뜻 |
|---|---|
| `GET /svc/prometheus/api/v1/query?query=<PromQL>[&time=<unix>]` | 즉시 질의. 응답은 Prometheus JSON `{status, data: {resultType: "vector", result: [{metric: {…}, value: [<unix>, "<값>"]}]}}` |
| `GET /svc/prometheus/api/v1/query_range?query=<PromQL>&start=<unix>&end=<unix>&step=<초>` | 구간 질의. `result[].values`가 `[<unix>, "<값>"]`의 배열 |
| 링크 `https://b-free.mangowhois.ai/grafana/` | 엔지니어용 Grafana(`BRAVO_GRAFANA_URL`). 같은 Access 앱 뒤에 있고, Grafana가 Access JWT(`Cf-Access-Jwt-Assertion`, 콘솔 AUD)로 Viewer 로그인을 만듦. Tempo 트레이스와 Loki 로그는 Grafana Explore에서 봄 |

- 콘솔 출처(`b-free.mangowhois.ai`)와 같은 출처이고, Access 쿠키로 들어옵니다. 프록시는 GET과 HEAD만 받습니다. POST는 403입니다(2026-11-02 22:11 KST 확인).
- 프록시 대상은 `BRAVO_PROMETHEUS_UPSTREAM=http://prometheus.bravo-observe.svc.cluster.local:9090`입니다. NetworkPolicy `prometheus-ingress`가 `bravo-control`의 콘솔 Pod만 9090으로 받습니다.

### 7.3 라벨

| 라벨 | 값 | 쓰는 곳 |
|---|---|---|
| `bravo_env` | `source-aws`, `target-onprem`, `target-gcp`, `platform` | 환경별 묶음. 모든 패널이 이 라벨로 나눔 |
| `service_name` | 데모 앱 `bravo-demo`, 플랫폼 서비스 이름 | 앱 지표 고르기 |
| `http_route`, `http_response_status_code`, `http_request_method` | OTel HTTP 의미 규약 | RED |
| `http_url`, `http_status_code`, `http_status_class`, `error_message` | `http_check` 수신기 | 플랫폼 서비스 상태 |
| `verdict`, `reason`, `source`, `destination`, `protocol` | Hubble(`source`, `destination`은 네임스페이스) | 흐름과 드롭 |
| `deployment_environment_name` | 앱이 스스로 붙인 값(`aws`, `gcp`, `onprem`) | 묶음에 쓰지 않음. `bravo_env`를 씀 |

### 7.4 패널과 PromQL

데모 앱 RED는 앱이 내는 OTel HTTP 서버 지표 `http.server.request.duration`(Prometheus 이름 `http_server_request_duration_seconds`)에서 계산합니다. kubelet 프로브 경로(`/healthz`, `/readyz`)는 뺍니다. 채팅 메시지 쓰기는 WebSocket으로도 들어오므로 HTTP RED에 다 잡히지 않습니다. 그래서 쓰기율은 앱의 `chat.writes` 카운터로 따로 봅니다.

| 패널 | PromQL | 단위 |
|---|---|---|
| 환경 연결(마지막 수신 뒤 경과) | `min by (bravo_env) (time() - timestamp(target_info{bravo_env!=""}))` | 초. `min`인 이유: `target_info`는 자원(Pod)마다 한 계열이고 OTLP는 끝 표시(staleness marker)가 없어, 교체된 Pod의 계열이 5분 동안 늙어 가며 남음. `max`는 그 옛 계열을 집어 연결된 환경을 "300초 전"으로 보임(2026-11-02 13:40Z, 13:54Z 온프렘 앱 롤아웃 때 실측) |
| 요청률 | `sum by (bravo_env) (rate(http_server_request_duration_seconds_count{service_name="bravo-demo", http_route!~"/healthz\|/readyz"}[1m]))` | 요청/초 |
| 오류율(5xx 비율) | `sum by (bravo_env) (rate(http_server_request_duration_seconds_count{service_name="bravo-demo", http_route!~"/healthz\|/readyz", http_response_status_code=~"5.."}[5m])) / sum by (bravo_env) (rate(http_server_request_duration_seconds_count{service_name="bravo-demo", http_route!~"/healthz\|/readyz"}[5m]))` | 0부터 1까지의 비율 |
| p50, p95, p99 | `histogram_quantile(0.95, sum by (bravo_env, le) (rate(http_server_request_duration_seconds_bucket{service_name="bravo-demo", http_route!~"/healthz\|/readyz"}[5m])))`(0.50, 0.99도 같은 식) | 초 |
| 상태 코드별 요청 수 | `sum by (bravo_env, http_response_status_code) (increase(http_server_request_duration_seconds_count{service_name="bravo-demo", http_route!~"/healthz\|/readyz"}[15m]))` | 건(15분) |
| 쓰기율 | `sum by (bravo_env, outcome) (rate(chat_writes_total{kind="message"}[1m]))` | 건/초 |
| 쓰기 지연 p95 | `histogram_quantile(0.95, sum by (bravo_env, le) (rate(chat_write_duration_milliseconds_bucket[5m])))` | ms(쓰기 정지 대기 포함) |
| 플랫폼 서비스 상태 | `max by (http_url) (httpcheck_status{http_status_class="2xx"})` | 1 정상, 0 비정상 |
| 플랫폼 서비스 오류 원인 | `max by (http_url, error_message) (httpcheck_error)` | 지난 5분 안의 실패 |
| 플랫폼 서비스 응답 시간 | `max by (http_url) (httpcheck_duration_milliseconds)` | ms |
| Hubble 흐름 | `sum by (bravo_env, verdict) (rate(hubble_flows_processed_total[5m]))` | 흐름/초 |
| Hubble 드롭(이유별) | `sum by (bravo_env, reason) (rate(hubble_drop_total[5m]))` | 드롭/초 |
| Hubble 드롭(1시간, 네임스페이스 쌍) | `sum by (bravo_env, reason, source, destination) (increase(hubble_drop_total[1h]))` | 건 |
| 에이전트 전송 실패 | `sum by (bravo_env) (increase(otelcol_exporter_send_failed_metric_points_total[5m]) or increase(otelcol_exporter_send_failed_spans_total[5m]))` | 건(5분) |
| Executor 단계 시간(사본) | `sum by (span_name) (traces_span_metrics_duration_milliseconds_sum{service_name="executor", span_name=~"cutover\\.stage\\..*"}) / sum by (span_name) (traces_span_metrics_duration_milliseconds_count{service_name="executor", span_name=~"cutover\\.stage\\..*"})` | ms(평균) |

상태 경로는 `api`와 `operator-mcp`가 `/readyz`, `collector`, `gate`, `rehearsal`, `executor`가 `/healthz`, `console`이 `/config.json`, OpenBao가 `/v1/sys/health`(200은 초기화 · 봉인 해제 · 활성)입니다.

**측정 예(2026-11-02 13:10:33 UTC).** 환경 넷 모두 30초 안에 수신했습니다(`target-gcp` 7초, `source-aws` 27초, `target-onprem` 5초, `platform` 3초). 출발지 앱의 지연은 p50 2.5 ms, p95 4.75 ms, p99 4.95 ms였고, 15분 동안 프로브를 뺀 요청은 200 응답 8.5건과 404 응답 7.4건이었습니다(`increase`의 외삽 값이라 소수). 이 요청은 확인을 위해 사람이 보낸 GET 20건이고, 부하 생성기 기록이 아닙니다. 출발지 Hubble은 FORWARDED 초당 146.6건, DROPPED 초당 0.11건(이유 `UNSUPPORTED_L3_PROTOCOL`, ICMPv6)이었습니다. 플랫폼 서비스 8개가 2xx였고, 바로 앞 5분 안에 Executor가 연결 거부로 한 번 실패한 기록(`httpcheck_error`)이 있었습니다.

### 7.5 컷오버 쓰기 정지와 단계 시간

판정 근거는 Prometheus가 아니라 Executor 기록입니다(07 6절 "관측은 판정 근거가 아님"). 콘솔은 API를 씁니다.

| 값 | 출처 |
|---|---|
| 쓰기 정지 ms | `GET /v1/plans/{id}`의 `run.write_pause_ms`. SSE `run.done`의 `write_pause_ms` |
| 단계 시간 | `run.stages[]` {`seq`, `name`, `started_at`, `ms`, `result`}. SSE `run.stage` |
| 영수증 | `run.receipt`(`receipt_id`, `verdict`, `body`). SSE `receipt.saved` |

Executor는 단계마다 `cutover.stage.<이름>` span을 냅니다. 플랫폼 Collector의 `span_metrics` 연결기가 이것을 `traces_span_metrics_duration_milliseconds`로 바꾸므로 7.4의 마지막 패널이 사본을 보입니다. 2026-11-02 22:10 KST에는 컷오버 실행 기록이 없어 이 패널은 비어 있습니다.

### 7.6 값이 없을 때

| 보이는 것 | 뜻 | 콘솔 문구 |
|---|---|---|
| 환경 연결 패널에 그 `bravo_env`가 없거나 120초를 넘음 | 그 환경 에이전트의 OTLP가 플랫폼에 오지 않음(링크, 토큰, 에이전트 중 하나) | 연결 안 됨: 환경 에이전트 OTLP 수신 없음(마지막 수신 N초 전) |
| 환경은 연결됐는데 요청률 0, 오류율 · 분위수 없음(NaN) | 그 창 안에 프로브 밖 요청이 없음 | 트래픽 없음 |
| 환경은 연결됐는데 `chat_writes_total` 계열이 없음 | 앱 카운터는 첫 쓰기 때 생김. Pod 시작 뒤 쓰기 0건 | 쓰기 0건(Pod 시작 뒤) |
| 어느 `bravo_env`의 Hubble 계열이 없음 | 그 환경의 Hubble 지표 수집이 끊김. 2026-11-02 14:14Z부터 넷 모두 들어옴: 출발지 EKS와 온프렘은 랜딩 존 Cilium `hubble.metrics`(온프렘은 14:06Z 무렵 `install.sh rke2 bravo-onprem cilium`을 다시 돌림), 플랫폼은 `deploy/platform/cilium`(플랫폼 갈래), GKE는 Dataplane V2 지표(`gcloud container clusters update --enable-dataplane-v2-metrics`, Terraform `enable_metrics = true`)를 `otel-agent` DaemonSet이 자기 노드의 anetd `:9965`에서 읽음(`overlays/gke/manifests/50-otel-agent.yaml`, 노드 서브넷 허용 `60-networkpolicies.yaml`). GKE는 같은 `hubble_*` 이름을 냄 | 연결 안 됨: <환경> Hubble 지표 수신 없음 |
| Executor 단계 패널이 빔 | 컷오버 실행이 아직 없거나 Executor가 span을 보내지 못함 | API의 `run.stages[]`를 봄 |

## 8. 비용 영수증 API(마무리 장면, NFR-8)

`GET /v1/cost`는 이 플랫폼이 쓰는 자원마다 금액 한 줄을 돌려줍니다. 줄마다 단가와 그 출처, 수량, 금액, 근거(청구 · 추정)가 있습니다. 계획을 주면 "이전 전 · 이전 중 · 이전 후"로 나누고 "이전 중 추가 비용"을 계산합니다. 지어낸 금액은 없습니다. 읽지 못한 값은 `null`이고, 이유가 `not_connected[]`에 있습니다. 형식은 2026-11-02 22:5x KST 코드(`cost/v1`)에서 옮겼습니다.

### 8.1 엔드포인트

| 요청 | 누가 | 뜻 |
|---|---|---|
| `GET /v1/cost[?plan_id=<uuid>][&start=<ISO>][&end=<ISO>]` | API(콘솔 출처 `b-free`, Access JWT, viewer 이상) | 콘솔이 부르는 경로. `plan_id`가 있으면 API가 플랫폼 DB에서 계획 생성 시각과 처음 `DONE` · `DONE_ROLLED_BACK`이 된 시각을 읽어 수집기에 넘김. 응답은 수집기 본문 그대로 |
| `GET /v1/cost?start=&end=&plan_id=&plan_created_at=&plan_done_at=&plan_state=&workload=` | 수집기(:8083, 클러스터 안, 콘솔 `/svc/collector/` 읽기 전용 프록시로도 닿음) | 같은 본문. 시각은 ISO 8601(UTC). 기본 기간은 이번 달 1일 00:00 KST(전날 15:00Z)부터 지금까지. 일별 묶음과 같은 시간대라 첫날도 24시간입니다. 계획을 주고 `start`가 없으면 계획 생성 24시간 전부터 |
| `python -m bravo_collector cost [--config] [--start] [--end] [--plan-id] [--plan-created-at] [--plan-done-at]` | 운영자 | 같은 본문을 표준 출력으로 |

오류: 수집기 422(시각 형식, `end <= start`, 62일 초과, `plan_done_at < plan_created_at`)는 API에서 422 `cost_bad_request`. 수집기가 설정되지 않으면 503 `collector_unconfigured`, 닿지 않으면 503 `collector_unreachable`(문구 "연결 안 됨: 수집기 비용 영수증"). 없는 계획은 404.

### 8.2 응답(`cost/v1`)

| 필드 | 뜻 |
|---|---|
| `schema_version`, `generated_at`, `inventory_observed_at`, `currency` | `cost/v1`, 응답 시각, 자원을 읽은 시각(5분 캐시), `USD` |
| `period` {`start`, `end`, `hours`, `days`}, `day_timezone` | 기간과 일별 묶음의 시간대(`+09:00`, KST · JST) |
| `plan` {`plan_id`, `state`, `workload`, `created_at`, `done_at`} | 계획(없으면 `null`). `done_at`이 `null`이면 아직 끝나지 않음 |
| `phases` {`before`, `during`, `after`} | 각 단계의 `start`, `end`. 이전 전 = 기간 시작부터 계획 생성까지, 이전 중 = 계획 생성부터 `DONE`(없으면 지금)까지, 이전 후 = `DONE`부터 기간 끝까지 |
| `summary.<단계>` | `label`, `start`, `end`, `hours`, `total`, `daily`(= `total` / `hours` × 24), `by_component` |
| `migration_extra` | `value`, `formula`("이전 중 전체 − 이전 중 출발지(component=source, 원래 내던 비용)"), `inputs` {`during_total`, `during_source`, `during_hours`}, `explain`(계산식 문자열), `lower_bound`(금액을 모르는 줄이 있으면 `true`) |
| `totals` | `total`(청구 + 추정 + 금액이 있는 정액), `estimated`, `billed`(태그 청구가 없으면 `null`), `flat`, `api_equivalent_not_billed`(합계에 넣지 않음), `unpriced_lines`, `by_component`, `by_environment`, `migration_extra` |
| `lines[]` | 8.3 |
| `daily[]` | {`date`(KST), `start`, `end`, `hours`, `by_component`, `total`, `active_lines`}. 합계에 들어가는 줄만. `active_lines`가 0이면 그날 돌던 자원이 없다는 뜻(빈 날은 사실이지 누락이 아님. 2026-11-01은 이 프로젝트 자원이 하나도 없었음: 가장 이른 자원이 11-02 10:31Z 생성) |
| `tag` | `component` 비용 할당 태그 상태: `status`(`Active` · `Inactive` · `NOT_REGISTERED`), `label`("활성" · "태그 활성화 대기"), `reason`, `activation_attempts[]` |
| `actuals` | Cost Explorer 결과: `status`, `period`, `by_service` {`scope`(계정 전체, 다른 프로젝트 포함, 합계에 넣지 않는 참고 값), `estimated`, `total`, `groups`, `daily`}, `cached_at`, `ttl_s`(3600), `from_cache`, `requests_this_response`, `charge_usd_on_fill` |
| `price_list_at`, `bill_received_at` | 가장 이른 단가 조회 시각, Cost Explorer를 읽은 시각 |
| `sources[]`, `not_connected[]`, `notes[]` | 출처별 상태, 연결 안 됨 목록 {`provider`, `what`, `reason`}, 범위 설명 |

### 8.3 줄(`lines[]`)

| 필드 | 뜻 |
|---|---|
| `line_id`, `provider`(`aws` · `gcp` · `cloudflare` · `llm`), `service`, `resource_type`, `resource_id`, `resource`(보이는 이름) | 자원 |
| `environment`(`source-aws` · `platform` · `target-gcp` · `edge` · `llm`), `component`(`source` · `platform` · `target` · `edge` · `llm`), `component_source`(`tag` 또는 `inferred:<규칙>`) | 어디에 붙는 비용인가. AWS는 `component` 태그, 없으면 이름 `bravo-<component>`, 붙은 인스턴스 순으로 정함. GCP 프로젝트는 목적지 전용이라 라벨이 없으면 `target` |
| `unit_price`, `unit`, `price_source` {`name`, `ref`(사용 유형 · SKU), `description`, `effective_date`, `url`, `fetched_at`} | 단가와 출처. AWS Pricing API, GCP Cloud Billing Catalog API, Cloudflare API, 공식 가격 페이지 |
| `quantity`, `quantity_unit` | 기간 안의 수량(시간, GB-month, GB, vCPU-hours, key-month, tokens …) |
| `subtotal` | `quantity × unit_price`. 모르면 `null` |
| `basis`, `basis_code` | `청구`(`billed`), `추정`(`estimated`, 정가 × 자원 자체 시각으로 본 실행 시간), `정액(구독)`(`flat`), `API 가격 환산(청구 아님)`(`api_equivalent`) |
| `in_total` | 합계에 들어가는가. 태그 청구 줄이 있으면 AWS 추정 줄은 `false`(이중 계산 방지). API 가격 환산 줄은 늘 `false` |
| `phases` {`before`, `during`, `after`: {`quantity`, `subtotal`}} | 계획이 있을 때만 |
| `running` {`from`, `to`, `state`}, `status`(`ok` · `not_connected`), `reason`, `note`, `plan_id`(에이전트 실행), `detail`(토큰 구성, LB 끝점) | 근거 |

### 8.4 무엇을 읽는가

| 범위 | 읽는 것(모두 읽기 호출) | 단가 |
|---|---|---|
| AWS(ap-northeast-2) | EC2 인스턴스(실행 중과, 종료 뒤 아직 보이는 것), EBS(붙은 인스턴스로 PVC 볼륨도), EKS, Regional NAT(시간 + CloudWatch 처리 GB, AZ 차원 합), NAT 공인 IPv4, ELB, S3(CloudWatch 일별 크기), KMS(별칭 `bravo-*`), ECR(이미지별 크기), Secrets Manager(메타데이터만), RDS, CloudTrail | AWS Pricing API On-Demand. 월 단가는 그 달 일수 × 24시간으로 시간 배분 |
| GCP(`b-free-target-20261102`) | GKE 클러스터 요금, 노드(vCPU, RAM), Hyperdisk Balanced(용량, 기본 3,000 IOPS · 140 MiB/s를 넘는 몫), Cloud NAT(VM 수 × 시간, NAT IP), 내부 Application LB 프록시, Artifact Registry 크기, Cloud KMS 키 버전 | Cloud Billing Catalog API. 월 단가는 730시간으로 시간 배분. GKE 무료 등급($74.40/월 크레딧)은 적용하지 않고 주석으로 둠 |
| Cloudflare | LB · 풀 · 끝점 수(`load_balancers`, `pools`), 구독 금액(`subscriptions`, Billing Read 필요), Zero Trust Free | API 구독 금액, Zero Trust는 Free 요금제라 0 |
| LLM | 구독(설정 파일, 공식 가격 페이지를 인용할 때만 금액), 에이전트 실행마다 기록된 토큰(Harbor `result.json`의 `agent_result.metadata.worker.tokens`) | 실행 금액은 Claude Agent SDK `total_cost_usd`(SDK 가격표)를 그대로 씀. 구독 경로라 청구되지 않음 |
| 청구(실제 금액) | Cost Explorer: `ListCostAllocationTags`, `GetCostAndUsage`(SERVICE, 태그가 Active면 TAG `component`도) | 태그가 Active가 되면 `aws:billed:<component>` 줄이 생기고, 하루 금액을 그날 24시간에 고르게 나눠 단계에 배분 |

자격(장기 키 없음, NFR-6 · D-26): AWS는 수집 역할의 웹 신원 + 인라인 정책 `cost-receipt-read`(`infra/envs/platform-aws/rke2.tf`). GCP는 Pod 자신의 서비스 계정 토큰(audience `bravo-platform-gcp-wif`)을 Workload Identity Federation(풀 `bravo-platform`, 공급자 `rke2`)으로 바꾸고 `bravo-cost-reader`(viewer 역할만)를 가장합니다(`infra/envs/target-gcp/platform_cost_reader.tf`, 자격 설정 `deploy/platform/control/config/gcp-wif-collector.json`). Cloudflare는 선택 Secret `collector-cloudflare`(`api_token`, `account_id`, `zone_id`, OpenBao에서 파이프로).

### 8.5 캐시와 요금

| 대상 | 캐시 | 이유 |
|---|---|---|
| Cost Explorer | 1시간, 기간은 늘 이번 달 1일부터 내일까지(UTC). 키가 하루에 한 번만 바뀜 | 요청마다 USD 0.01. 한 번 채울 때 2건(태그가 Active면 3건) |
| 자원 목록 | 5분 | Describe · List는 무료지만 느림(처음 10초에서 60초) |
| 단가 | 24시간 | 가격표는 달 단위로 바뀜 |

키마다 잠금이 있어 콘솔이 여러 번 동시에 불러도 같은 호출을 한 번만 합니다(`tests/test_cost.py`). 자원 목록과 Cost Explorer는 만료되면 지난 값을 바로 돌려주고 뒤에서 한 번만 다시 채웁니다. 콘솔 프록시가 20초만 기다리기 때문입니다. 수집기는 뜰 때 한 번 미리 채웁니다(Cost Explorer 2건). 그래서 다시 뜰 때마다 USD 0.02가 듭니다.

### 8.6 값이 없을 때

| 보이는 것 | 뜻 |
|---|---|
| `tag.label` = "태그 활성화 대기" | 청구 데이터가 아직 `component` 태그를 모름. `deploy/platform/scripts/cost-tag.sh activate`는 "Tag keys not found: component"로 거절됨(2026-11-02 13:16Z, 13:46Z, 팀장 승인). 태그 단 자원이 청구에 잡힌 뒤 최대 24시간. 그때까지 컴포넌트별 실제 금액은 없고 자원별 추정을 씀 |
| `actuals.by_service`만 있음 | 계정 전체 실제 금액(다른 프로젝트 포함). 합계에 넣지 않음 |
| S3 줄 `subtotal: null` | CloudWatch `BucketSizeBytes`가 하루 단위라 만든 날에는 값이 없음 |
| Cloudflare LB 줄 `unit_price: null` | 토큰에 Billing Read가 없음(`403 Authentication error`). Cloudflare 문서는 LB 금액을 대시보드에서만 보임 |
| LLM 구독 `unit_price: null` | Claude 구독 등급이 기록되지 않음. ChatGPT 가격 페이지는 2026-11-02 HTTP 403이고, Pro 등급이 둘이라는 보도가 있어 금액을 정하지 않음 |
| "에이전트 실행별 토큰 사용량" 연결 안 됨 | 플랫폼 리허설 서비스가 Trial 결과를 emptyDir에만 두어 수집기가 읽을 곳이 없음(`BRAVO_LLM_USAGE_DIR` 없음). 노트북 실행 기록에서는 읽힘 |
| GCP "청구(실제 금액)" 연결 안 됨 | Cloud Billing 내보내기(BigQuery)가 없음. GCP 줄은 모두 추정 |
| 삭제된 자원 | Describe가 더는 돌려주지 않는 자원(종료 1시간 뒤의 인스턴스, 지운 EKS `bravo-platform`)은 추정에서 빠짐. 태그 청구가 켜지면 그 몫이 청구 줄에 들어감 |

**측정 예(2026-11-02 13:45Z, 노트북 실행, 운영자 자격 · Cloudflare 토큰 · 실행 기록 포함).** 기간 11/1 00:00Z부터 13:44Z까지 63줄, 합계 추정 $2.51(출발지 $0.89, 플랫폼 $0.84, 목적지 GKE $0.78, edge $0), 금액을 모르는 줄 8개, API 가격 환산(청구 아님) $0.39(실행 2개, 토큰 42,154개와 62,471개). Cost Explorer 계정 전체는 $1.02(추정값, 하루 지연). 플랫폼 수집기 Pod(웹 신원, 13:41Z)는 48줄, 합계 $1.99였습니다. 그때는 GCP 가장 권한이 퍼지기 전이라 GCP가 연결 안 됨이었고, 13:46Z에 같은 신원으로 GKE · Compute · KMS · Artifact Registry · Billing Catalog가 모두 200인 것을 확인했습니다. 계획이 아직 없어(플랫폼 DB `plans` 0행) 단계 배분 값은 `tests/test_cost.py`의 계산 시험으로만 확인했습니다.

## 9. 통제 예외(S1 5b)

스펙 S1 예외 흐름 5b: "목적지에서 대체할 수 없는 통제가 있다 → 승인자가 예외를 승인하면 '예외 승인'으로 따로 표시하고 동등 판정에서 뺀다." 5a는 그대로입니다. UNKNOWN은 예외로 바꿀 수 없고 늘 막습니다. 인수 조건 "예외 승인 없는 FAIL이나 UNKNOWN이면 이전 후보 등록이 막힌다"의 "예외 승인"이 이 절입니다. 이 절은 2026-11-02 23:3x KST에 코드보다 먼저 고정했습니다. 코드가 다르면 코드를 고칩니다.

실제 사례: GKE `target-gcp`의 OBJ-06(평문 HTTP 거부). GCS XML API는 `http://`를 받고, 막는 조직 정책 `storage.secureHttpTransport`는 조직이 없는 이 프로젝트에 걸 수 없습니다(`infra/envs/target-gcp/README.md`).

### 9.1 무엇에 결속되는가

예외 하나는 아래 값에 묶입니다. 하나라도 바뀌면 예외는 효력을 잃습니다.

| 값 | 출처 |
|---|---|
| 계획 `plan_id` | 경로 |
| 통제 ID `control_id` | 게이트 판정 `controls[].control_id`(질문 ID, 예: `OBJ-06`) |
| 목적지 환경 `target_environment_id` | 계획의 목적지. DB 트리거가 같은지 확인 |
| 요청 때 본 게이트 판정 `gate_verdict_id` | 계획의 마지막 판정(`last_gate.verdict_id`)이어야 요청 가능 |
| 값 쌍 `src`, `dst` | 그 판정의 행. `dst`는 `FAIL`, `src`는 `PASS` 또는 `FAIL`만 |
| 증거 digest `evidence_digest` | 그 판정 행의 `evidence_digest`(목적지 증거의 sha256, 9.5) |
| 사유 `reason`, 만료 `expires_at` | 요청 본문 |

예외를 걸 수 있는 행은 마지막 게이트 판정의 `controls[]` 가운데 `verdict == "BLOCK"`, `dst == "FAIL"`, `src`가 `PASS` 또는 `FAIL`, `evidence_digest`가 `null`이 아닌 행입니다. 증거가 없는 행(Executor가 낸 `DB-ANON-READ`, `DB-AUTH`)은 걸 수 없습니다.

### 9.2 상태

| `status` | 뜻 | 콘솔 문구(제안) | 다음 |
|---|---|---|---|
| `requested` | operator가 요청함 | 승인 대기 | `approved` · `rejected` · `stale` · `expired` |
| `approved` | approver가 승인함. 만료 전이고 증거가 같으면 게이트 판정에서 그 행이 `EXCEPTION` | 예외 승인 | `stale` · `expired` |
| `rejected` | approver가 거절함 | 거절됨 | 끝 |
| `stale` | 측정 결과가 바뀜(`stale_reason`: `evidence_changed` · `verdict_changed`) | 무효(증거 바뀜) | 끝. 새로 요청 |
| `expired` | `expires_at`이 지남 | 만료 | 끝. 새로 요청 |

- `stale`은 게이트를 다시 돌렸을 때 그 통제가 양쪽 모두 측정됐고(UNKNOWN 아님) 값 쌍이나 증거 digest가 다를 때 API가 기록합니다. 목적지를 고쳐 PASS가 되면 `verdict_changed`입니다.
- 그 통제가 UNKNOWN이거나 판정에 없으면(게이트에 못 닿음 등) 예외는 그 판정에 쓰이지 않고 상태도 그대로입니다. 그 판정은 UNKNOWN으로 막힙니다(5a).
- `expired`는 요청 · 승인 · 게이트 실행 때 API가 기록합니다. 목록 응답은 아직 기록 전이어도 `expires_at`이 지났으면 `expired`로 보입니다.
- 계획마다 통제 하나에 열린 예외(`requested` · `approved`)는 하나뿐입니다.

### 9.3 엔드포인트

인증 · 오류 형식은 4절과 같습니다(Access JWT, 콘솔 AUD). 쓰기에는 `Idempotency-Key`(8–200자)가 필요합니다. MCP에는 없습니다.

| 요청 | 누가 | 응답 |
|---|---|---|
| `GET /v1/plans/{id}/exceptions` | viewer 이상 | 200 `{"plan_id", "exceptions": [예외, …]}` 최신순 |
| `POST /v1/plans/{id}/exceptions` | 콘솔 operator | 201 예외(`status: "requested"`) |
| `POST /v1/plans/{id}/exceptions/{exception_id}/approve` | 콘솔 approver | 200 예외(`status: "approved"`) |
| `POST /v1/plans/{id}/exceptions/{exception_id}/reject` | 콘솔 approver | 200 예외(`status: "rejected"`) |

요청 본문:

```json
{"control_id": "OBJ-06", "gate_verdict_id": "<last_gate.verdict_id>",
 "evidence_digest": "sha256:<last_gate.controls[].evidence_digest>",
 "reason": "GCS XML API는 평문 HTTP를 받음. 조직이 없어 storage.secureHttpTransport를 걸 수 없음. 보완: 노드 바깥 80 차단, 앱 NetworkPolicy 443만",
 "expires_at": "2026-11-09T18:00:00+09:00"}
```

- `reason`: 10–2000자. 왜 대체할 수 없는지와 보완 통제를 씁니다.
- `expires_at`: 시간대가 있는 ISO 8601. 지금부터 5분 뒤 ~ 30일 뒤.

승인 본문 `{"evidence_digest": "sha256:…", "note": "<0–2000자, 생략 가능>"}`. `evidence_digest`는 승인자가 화면에서 본 값입니다. 거절 본문 `{"note": "<0–2000자, 생략 가능>"}`.

예외 객체(목록 · 요청 · 승인 · 거절 응답 모두 같음):

```json
{"exception_id": "<uuid>", "plan_id": "<uuid>", "control_id": "OBJ-06",
 "target_environment": {"environment_id": "<uuid>", "name": "target-gcp"},
 "gate_verdict_id": "<uuid>", "src": "PASS", "dst": "FAIL", "evidence_digest": "sha256:…",
 "reason": "…", "expires_at": "2026-11-09T09:00:00+00:00",
 "status": "approved", "stale_reason": null,
 "requested_by": "<operator 이메일>", "requested_at": "…",
 "decided_by": "<approver 이메일>", "decided_at": "…", "decision_note": null,
 "applied": true}
```

- `applied`: 계획의 마지막 게이트 판정이 이 예외로 그 행을 `EXCEPTION`으로 바꿨으면 `true`.
- 승인해도 지금 판정은 바뀌지 않습니다. operator가 게이트를 다시 돌리면(`POST /v1/plans/{id}/gate`) 새 판정에 반영됩니다. 그때 게이트가 증거를 다시 재고 digest를 다시 비교합니다.

직무 분리: 승인 · 거절하는 사람 ≠ 요청한 사람 ≠ 계획 작성자. approver 역할이 있어야 합니다. API가 먼저 보고 DB 트리거(`bravo_control_exceptions_guard`)가 다시 확인합니다.

오류:

| 코드 | 이유 | `detail` |
|---|---|---|
| 400 | `idempotency_key_required` | |
| 403 | `insufficient_scope`(역할 · 채널), `separation_of_duties` | |
| 404 | `plan_not_found`, `exception_not_found` | |
| 409 | `state_mismatch`(계획이 CUTOVER · FAILBACK · DONE · DONE_ROLLED_BACK · SAFE_HOLD · ABORT_PRE_WRITE) | `state` |
| 409 | `exception_stale` | `cause`: `no_gate_verdict`, `gate_verdict_superseded`(요청의 판정이 마지막 판정이 아님), `evidence_changed`, `verdict_changed`, `evidence_mismatch`(승인 본문 digest ≠ 예외), `exception_expired` |
| 409 | `exception_open`(그 통제에 열린 예외가 있음) | `exception_id`, `status` |
| 409 | `exception_not_pending`(승인 · 거절할 수 없는 상태) | `status` |
| 409 | `idempotency_key_reuse` | |
| 422 | `exception_not_allowed` | `cause`: `unknown`(src나 dst가 UNKNOWN, 5a), `not_failed`(dst가 FAIL이 아님), `evidence_missing`(증거 digest 없음), `control_not_in_verdict` |
| 422 | `exception_expiry_invalid` | `min`, `max` |
| 422 | 본문 형식 오류(FastAPI 기본 `detail` 목록) | |

### 9.4 게이트 판정에서

`POST /v1/plans/{id}/gate`, 시스템 재대조(`change_recheck`), WATCH 재시험 모두 같은 규칙입니다. 규칙 판 `gate-rules/2026-11-02.2`.

- 행이 `dst == "FAIL"`, `src`가 `PASS` 또는 `FAIL`이고, 그 계획 · 그 목적지 · 그 통제의 `approved` 예외가 만료 전이며 `src` · `dst` · `evidence_digest`가 모두 같으면: 그 행은 `verdict: "EXCEPTION"`, `reason: "exception_approved"`, `exception_id`. `reasons[]`에 넣지 않습니다(동등 판정에서 뺌).
- 나머지 행은 그대로입니다. UNKNOWN은 늘 `BLOCK` · `unknown`입니다. 준비 상태(`readiness:*`)와 수집 오류도 그대로 막습니다.
- 판정 `verdict`는 여전히 `PASS` · `BLOCK`입니다. `EXCEPTION` 행만 남고 나머지가 통과하면 `PASS`입니다.
- `input_digest`에 적용한 예외(`exception_id`, `control_id`, `src`, `dst`, `evidence_digest`)가 들어갑니다. 같은 관측 + 같은 예외 → 같은 판정.

판정 응답 · `gate.verdict` 이벤트 · `GET /v1/plans/{id}`의 `last_gate`에 더한 키:

| 키 | 뜻 |
|---|---|
| `controls[].evidence_digest` | 목적지 증거 digest(게이트 보고서의 `dst_evidence_digest`). 증거가 없으면 `null` |
| `controls[].exception_id` | `EXCEPTION`인 행만, 아니면 `null` |
| `controls[].verdict` | `PASS` · `BLOCK` · `EXCEPTION` |
| `exceptions[]` | 이 판정에 적용한 예외. `{"exception_id", "control_id", "src", "dst", "evidence_digest", "expires_at", "approved_by"}`. 콘솔은 "예외 승인"으로 따로 보임 |

### 9.5 게이트 서비스 쪽(`platform/gate`)

- `GET /v1/controls/report`의 `questions[]` 행에 `src_evidence_digest`, `dst_evidence_digest`를 더합니다. 값은 `"sha256:" + sha256(JSON(증거 객체))`. 증거 객체는 `src_evidence` · `dst_evidence` 그대로(`{"verdict", "reason", "evidence"}`), JSON은 키 정렬 · 구분자 `,` `:` · UTF-8. 시각은 증거에 없으므로 같은 관측이면 같은 값입니다.
- `GET /v1/controls`(4개 키 계약)는 바뀌지 않습니다.
- 예외는 게이트 서비스가 모릅니다. `EXCEPTION`은 API가 판정을 조립할 때만 나옵니다.
- API는 이제 `GET /v1/controls/report?source=<출발지 환경 이름>&target=<목적지 환경 이름>`을 읽습니다(전에는 인자 없는 `/v1/controls`라 게이트가 422로 답했습니다). 질문 행을 `{control_id, src, dst, evidence_digest}`로 받습니다. 시간 제한 `BRAVO_GATE_TIMEOUT_S`(기본 60초). 게이트가 모르는 환경 이름이면 `gate_controls_unknown_environment`로 막습니다. 같은 통제 ID가 Executor와 게이트 양쪽에서 오면 지금처럼 UNKNOWN입니다.

### 9.6 이벤트

| kind | data |
|---|---|
| `exception.requested` | `exception_id`, `control_id`, `target_environment_id`, `gate_verdict_id`, `src`, `dst`, `evidence_digest`, `expires_at` |
| `exception.approved` | `exception_id`, `control_id`, `evidence_digest`, `expires_at` |
| `exception.rejected` | `exception_id`, `control_id` |
| `exception.stale` | `exception_id`, `control_id`, `cause`(`evidence_changed` · `verdict_changed`), `verdict_id`(바뀐 것을 본 판정), `was` {`src`, `dst`, `evidence_digest`}, `now` {`src`, `dst`, `evidence_digest`} |
| `exception.expired` | `exception_id`, `control_id`, `expires_at` |

봉투 `actor`는 요청 · 승인 · 거절이 콘솔 사람, `stale` · `expired`는 그 게이트를 돌린 주체(시스템 재대조면 `system`)입니다.

### 9.7 DB(마이그레이션 0008)

- 표 `control_exceptions`: 9.1의 값, `status`, `stale_reason`, `requested_by`, `requested_at`, `decided_by`, `decided_at`, `decision_note`, `updated_at`. RLS는 다른 표와 같습니다(`tenant_isolation` to `bravo_api` · `bravo_mcp`, 소유자 `owner_definer`). `bravo_api`는 읽기 · 쓰기, `bravo_mcp`는 읽기와 `stale` · `expired` 기록만(MCP `gate_run`이 같은 처리기를 씀). Executor · T31 워커 역할은 권한 없음.
- 트리거 `bravo_control_exceptions_guard`: 요청 때 요청자 = 세션 주체, 목적지 = 계획의 목적지, 판정 = 계획의 마지막 판정이고 그 행이 `dst = FAIL`, `src ∈ {PASS, FAIL}`, 같은 digest인지 확인. 결속 값은 바꿀 수 없음. 허용 전이만(9.2). 승인 · 거절은 결정자 = 세션 주체, approver 멤버십, 결정자 ≠ 요청자 ≠ 계획 작성자. 승인은 만료 전이고 마지막 판정의 그 행이 아직 같을 때만.
- 부분 유일 색인: 계획 · 통제마다 `requested` · `approved` 하나.
- `gate_verdicts.exceptions`(jsonb, 기본 `[]`): 그 판정에 적용한 예외.

## 스펙 밖

`GET /v1/plans`, `GET /v1/environments`, `POST /v1/plans/{id}/changes`, `GET /v1/cost`(T1 "component 태그별 비용 조회"를 API로 낸 것), JIT 역할 파일, `envs/<이름>/…` 비밀 참조는 스펙 §8.9에 없습니다. 2026-11-02 코디네이터 지시(초보 고객이 첫 화면에서 모든 것을 보고 시작)로 넣었습니다. 회의에서 `docs/spec/DEVIATIONS.md`에 올릴지 정합니다.
