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
| 10. SHIFT: 트래픽 전환 10/50/100과 컷오버 시작(S3, FR-8, T17) | T10 API ↔ T12 · T17 Executor, T21 콘솔 | `platform/api/src/bravo_api/statemachine.py`, `shift.py`, 마이그레이션 `0009_shift.sql`, `platform/executor/src/bravo_executor/front.py`, `front_api.py`, `approvals.py` |
| 11. 무결성 영수증(`verdict_parts`, hash_rule v1, 검사 범위) | T12 Executor → T10 API → T21 콘솔 | `platform/executor/src/bravo_executor/cutover.py`, `hashing.py`, `attachment_copy.py`, `platform/api/src/bravo_api/events.py` |
| 12. 기록 재생(`mode: recorded`) | T10 API → T21 콘솔 | `platform/api/src/bravo_api/service.py`(`replay`), `rest.py` |

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
- `mode`: SSE 스트림은 `live`만 씁니다. `recorded`는 12절의 기록 재생 응답에만 나옵니다. 재생은 `events` 표에 쓰지 않습니다.
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
| `tool.denied` | `rehearsal_id`, `trial_id`, `seq`, `tool`, `rule`, `origin`(`agent` · `probe`: 운영자 데모 probe는 에이전트 행동이 아님), `at` |
| `trial.progress` | `rehearsal_id`, `trial_id`, `role`, `phase`(provisioning · agent_running · proposed · verifying · done), `elapsed_s`, `phase_s`, `tool_summary`(주체별 허용 · 거부 수). 단계마다 한 번(마이그레이션 0010) |
| `trial.verdict` | `rehearsal_id`, `trial_id`, `attempt`, `verdict`(PASS · FAIL · INFRA_ERROR), `reason`, `artifact_digest`, `verifier` |
| `change.requested` | `change_id`, `rehearsal_id`, `role`, `plan_revision`, `artifact_digest`, `diff_digest`, `policy_diff_digest`, `policy_targets[]` {`type`, `name`} |
| `change.stage` | `change_id`, `seq`, `phase`(prepare · apply), `name`, `started_at`, `ms`, `result`(OK · FAIL · REFUSED · NOT_CONNECTED · SKIPPED), `detail`(비밀 없음) |
| `change.prepared` | `change_id`, `image_repository`, `image_digest`, `sbom_digest`, `scan` {`scanner`, `version`, `db`, `counts`, `policy`}, `build`, `diff_digest`, `policy_diff_digest`, `policy_targets`, `artifact_digest` |
| `change.applied` | `change_id`, `approval_id`, `approved_image_digest`, `deployed_digest`, `policies_applied`, `gate_recheck`, `signature` |
| `change.prepare_failed` · `change.refused` · `change.failed` | `change_id`, `status`, `reason` |
| `run.stage` | `seq`, `name`, `ms`, `result`, `started_at`(봉투 `run_id`). 이름이 `front.`로 시작하는 단계와 `failback.8_front_to_src`에는 `front`(10.4절)를 더함 |
| `run.done` | `kind`(shift · cutover · failback), `status`(DONE · ABORT_PRE_WRITE · SAFE_HOLD), `write_pause_ms`(shift는 `null`) |
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
| `rule_version` | `gate-controls/2026-11-03.2`(질문 22개) |
| `source`, `target` | 환경 ID |
| `gate_verdict`, `blocked_controls[]` | PASS · BLOCK, BLOCK인 통제 ID |
| `questions[]` | 질문마다: `control_id`(질문 ID), `parent_control`, `question`, `src`, `dst`, `src_status`, `dst_status`(충족 · 보완 필요 · 미검증), `verdict`, `reason`(`regression` · `fail_both` · `unknown` · `not_equivalent`), `src_evidence`, `dst_evidence`, `src_evidence_digest`, `dst_evidence_digest`(9.5). 2026-11-03.2부터 시험 방법: `method`(`behavioural` · `config` · `simulation`, 두 방법이면 `behavioural+config`, 양쪽이 다르면 `mixed`), `src_method`, `dst_method`, `src_tool`, `dst_tool`, `spec_test`(답하는 스펙 §8.5 시험). 대응표는 [16-gate-test-map.md](16-gate-test-map.md) |
| `controls[]` | 통제마다(SEC-01, ENC-01, AUD-01, NET-01, IAM-01, DR-01): `control_id`, `aliases`, `name_ko`, `question`, `src`, `dst`, `src_status`, `dst_status`, `verdict`, `reason`, `questions[]` |
| `input_digest` | 관찰 결과(시각 제외)의 sha256. 같은 입력 → 같은 값 |
| `test_version`, `collected_at`, `expires_at` | 시험기 판, 수집 시각, 만료 시각(`BRAVO_EVIDENCE_TTL_S`, 기본 3600초). 만료된 증거로는 승인하지 않음 |
| `cis_benchmark` | kube-bench 요약, **판정에 넣지 않음**: `{judged: false, label: "근사(approximate)", src, dst}`. 쪽마다 `status`(`current` · `expired` · `not_run`), `benchmark`, `collected_at`, `expires_at`(7일), `totals`, `fails[]`(`id`, `desc`, `judgement`: `fixed` · `exception` · `not_applicable` · `unjudged`, `reason`, `compensating`, `nodes`), `runs[]`(원본 경로). 출처는 ConfigMap `gate-cis-evidence`(`docs/evidence/kube-bench/cis-summary.py`) |

증거 객체(`src_evidence`, `dst_evidence`): `{"verdict": "PASS|FAIL|UNKNOWN", "reason": "<코드>", "evidence": {...}}`. `evidence`에는 비밀이 없습니다(오류 문자열도 가림, 감사 38). 여러 하위 관찰은 `{"parts": [...]}`. 절이 없는 쪽은 `UNKNOWN` · `not_configured`로 남고 게이트를 막습니다(감사 B16).

**플랫폼에서 무엇으로 재나(2026-11-03, 규칙 `gate-controls/2026-11-03.2`).** 플랫폼 게이트(`gate.bravo-control.svc:8084`)는 레지스트리 `deploy/platform/control/config/environments.yaml`의 `source-aws`, `target-onprem`, `target-gcp`를 씁니다. 환경마다 수집기와 다른 자기 읽기 신원입니다(`deploy/platform/link/gate-rbac/README.md`).

| 환경 | K8s | DB(역할 `gate_ro`) | 첨부 저장소 | 비밀 · 감사(`platform`) |
|---|---|---|---|---|
| `source-aws` | `src-k8s`, EKS 토큰(역할 `gate-ro`, 그룹 `bravo-gate`) | `src-db` | S3, 역할 `gate-ro`(canary 한 개 GET, 암호화 · 버킷 정책 읽기), OBJ-05는 권한 없는 역할 `gate-other`, OBJ-04는 앱 역할의 IAM 시뮬레이션 | `eks`: Secrets Manager · KMS · EKS · CloudTrail(역할 `gate-ro`) |
| `target-onprem` | `dst-k8s`, SA `kube-system/bravo-gate` | `dst-db` | RGW, `dst-s3`(TLS) · `dst-s3http`(평문 수신, OBJ-06), 앱 키 · `bravo-gate-other` 키 | `rke2`: kube-apiserver `/metrics` · 정적 Pod 플래그, 플랫폼 OpenBao |
| `target-gcp` | `gke-k8s`(클러스터 CA는 container API), Google SA `bravo-gate-ro`(WIF) | `gke-db` | GCS XML API, 앱 HMAC · `gate-other` HMAC. OBJ-ENC는 JSON API `buckets.get` | `gke`: container API `databaseEncryption`, Cloud Audit Logs, 플랫폼 OpenBao |

- **ID 변경(공급자 중립).** `SEC-SECRET-READ-AWS` → `SEC-SECRET-STORE-READ`, `SEC-ENC-CMK` → `SEC-SECRET-AT-REST`, `AUD-PLATFORM` → `AUD-CONTROL-PLANE`. 세 질문은 절 `platform`을 쓰고, 같은 질문을 세 공급자에 던집니다. 시험 방법은 `platform/gate/README.md` 표입니다. 옛 ID에 묶인 통제 예외는 새 행과 맞지 않습니다(새 ID로 다시 요청).
- **실행 두 가지.** `GET /v1/controls/report`는 읽기만 합니다. `POST /v1/controls/report` `{"source", "target"}`는 계획 평가 실행이고 OBJ-03 · OBJ-04 쓰기 시험을 게이트의 `_gate/` 키에만 하고 지웁니다. 플랫폼 API(`GateControlsClient`)는 POST를 씁니다.
- **OBJ-04(AWS).** 게이트에 앱 자격(IRSA 역할)을 주지 않습니다. `iam:SimulatePrincipalPolicy`로 앱 역할이 canary `s3:GetObject`, 시험 키 `s3:PutObject`(버킷 정책 포함), 버킷 KMS 키 `kms:GenerateDataKey` · `kms:Decrypt`(키 정책 포함, `kms:ViaService`=S3)를 할 수 있는지 봅니다. RGW · GCS는 게이트가 가진 앱 키로 실제 PUT · 삭제입니다.
- **NET-PATHS.** 앱 네임스페이스에 앱 Deployment의 라벨 · 이미지로 짧은 시험 Pod를 띄워 잽니다(SA 토큰 없음, 늘 준비 안 됨, 90초, 지움). 대조 `chat-pgbouncer.chat.svc:6432`는 연결, 거부 `169.254.169.254:80` · `1.1.1.1:80`은 거부 · 시간 초과여야 PASS.
- **새 질문 셋(2026-11-03.2, 리뷰 B12 · P10 · P16).** `SEC-GIT-PLAINTEXT`(SEC-01): 게이트 초기화 컨테이너가 수집기와 같은 고정 커밋 · 읽기 전용 배포 키로 받은 고객 저장소를 gitleaks 8.30.1로 검사(발견은 규칙 · 파일 · 줄만). `OBJ-PLAIN-EGRESS`(ENC-01): NET-PATHS와 같은 시험 Pod에서 저장소 TLS 포트는 연결, 평문 포트는 거부 · 시간 초과, 앱 Deployment의 `S3_ENDPOINT_URL`이 https(미설정은 SDK 기본 https). GKE OBJ-06 예외의 대체 통제이고, 예외 요청의 `compensating_control`에 이 ID를 적습니다. `NET-EXTERNAL-SCAN`(NET-01): 플랫폼에서 레지스트리 `network.public_endpoints`의 주소마다 TCP connect 스캔(포트 32개), 열린 포트가 `allow` 안에 있고 허용 포트가 하나 이상 답해야 PASS. 레지스트리 키는 `network.object_store_tls_path` · `object_store_plain_path` · `app_endpoint_env` · `public_endpoints` · `scan_ports`, 새 절 `repository.root`.
- **OBJ-ENC 객체 수준.** 게이트가 앱 키를 가진 저장소(RGW, GCS)는 객체를 모두 나열하고 HEAD로 SSE 헤더(`x-amz-server-side-encryption: aws:kms`, GCS `x-goog-encryption-kms-key-name`)를 봅니다. 하나라도 없으면 FAIL `plaintext_objects_present`. 출발지 S3는 목록 권한이 없어 버킷 수준만(`object_level.state: not_checked_no_list_right`).
- K8s 권한은 질문이 부르는 것뿐입니다. SubjectAccessReview 만들기, 네임스페이스 읽기, `/metrics`, ns `chat`의 NetworkPolicy 목록 · CNPG Cluster 읽기 · Deployment 읽기 · 시험 Pod 만들기/지우기/로그. Secret `get`은 없습니다(SEC-SECRET-READ-K8S 증거 `secret_existence: not_checked_no_get_right`).
- 레지스트리 키(`platform/gate/README.md`): `kubernetes.provider`(`eks` · `gke`), `api_server`, `gke_cluster`, `postgres.dial_host`, `object_store.ca_file` · `plain_http_endpoint_url` · `app_principal_arn` · `write_probes`, `network.probe_from: pod` · `app_deployment`, `platform.kind` · `gke_cluster` · `apiserver_pod` · `secret_store`.

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

스펙 S1 예외 흐름 5b: "목적지에서 대체할 수 없는 통제가 있다 → 승인자가 예외를 승인하면 '예외 승인'으로 따로 표시하고 동등 판정에서 뺀다." 5a는 그대로입니다. UNKNOWN은 예외로 바꿀 수 없고 늘 막습니다. 인수 조건 "예외 승인 없는 FAIL이나 UNKNOWN이면 이전 후보 등록이 막힌다"의 "예외 승인"이 이 절입니다. 이 절은 2026-11-02 23:3x KST에 코드보다 먼저 고정했고, 구현(커밋 `f1cd770`, 시험 `platform/api/tests/test_exceptions.py`)이 따릅니다. 증거는 `docs/evidence/control-exception/`.

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
| 사유 `reason`, 대체 통제 `compensating_control`, 만료 `expires_at` | 요청 본문. 스펙 §8.5 "대체 불가 예외는 승인 · 기간 · 대체 통제와 함께 '예외 승인'으로 따로 표시" |

예외를 걸 수 있는 행은 마지막 게이트 판정의 `controls[]` 가운데 `verdict == "BLOCK"`, `dst == "FAIL"`, `src`가 `PASS` 또는 `FAIL`, `evidence_digest`가 `null`이 아닌 행입니다. 증거가 없는 행(Executor가 낸 `DB-ANON-READ`, `DB-AUTH`)은 걸 수 없습니다. 그 판정의 증거가 만료됐으면(`last_gate.evidence_expires_at`이 지남) 요청 · 승인하지 않습니다(스펙 §8.5 "만료된 증거로는 승인하지 않는다"). 게이트를 다시 돌리면 됩니다.

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
- `expired`는 새 요청과 게이트 실행 때 API가 기록합니다. 목록 응답은 아직 기록 전이어도 `expires_at`이 지났으면 `expired`로 보입니다. 만료된 요청을 승인 · 거절하면 409 `exception_stale`(`exception_expired`)입니다.
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
 "reason": "GCS XML API는 평문 HTTP를 받음. 조직이 없어 storage.secureHttpTransport를 걸 수 없음",
 "compensating_control": "노드에서 바깥 80 차단 방화벽, 앱 NetworkPolicy는 443만 허용",
 "expires_at": "2026-11-09T18:00:00+09:00"}
```

- `reason`: 10–2000자. 왜 목적지에서 대체할 수 없는지.
- `compensating_control`: 10–2000자. 대신 둔 통제.
- `expires_at`: 시간대가 있는 ISO 8601. 지금부터 5분 뒤 ~ 30일 뒤.

승인 본문 `{"evidence_digest": "sha256:…", "note": "<0–2000자, 생략 가능>"}`. `evidence_digest`는 승인자가 화면에서 본 값입니다. 거절 본문 `{"note": "<0–2000자, 생략 가능>"}`.

예외 객체(목록 · 요청 · 승인 · 거절 응답 모두 같음):

```json
{"exception_id": "<uuid>", "plan_id": "<uuid>", "control_id": "OBJ-06",
 "target_environment": {"environment_id": "<uuid>", "name": "target-gcp"},
 "gate_verdict_id": "<uuid>", "src": "PASS", "dst": "FAIL", "evidence_digest": "sha256:…",
 "reason": "…", "compensating_control": "…", "expires_at": "2026-11-09T09:00:00+00:00",
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
| 409 | `exception_stale` | `cause`: `no_gate_verdict`, `gate_verdict_superseded`(요청의 판정이 마지막 판정이 아님), `evidence_changed`, `verdict_changed`, `not_measured`(승인 때 마지막 판정에서 그 통제가 UNKNOWN이거나 없음), `evidence_expired`(마지막 판정의 증거 만료), `evidence_mismatch`(승인 본문 digest ≠ 예외), `exception_expired`, `db_recheck`(DB 트리거가 결속 불일치를 다시 잡음) |
| 409 | `exception_open`(그 통제에 열린 예외가 있음) | `exception_id`, `status` |
| 409 | `exception_not_pending`(승인 · 거절할 수 없는 상태) | `status` |
| 409 | `idempotency_key_reuse` | |
| 422 | `exception_not_allowed` | `cause`: `unknown`(src나 dst가 UNKNOWN, 5a), `not_failed`(dst가 FAIL이 아님), `evidence_missing`(증거 digest 없음), `control_not_in_verdict` |
| 422 | `exception_expiry_invalid` | `min`, `max` |
| 422 | 본문 형식 오류(FastAPI 기본 `detail` 목록) | |

### 9.4 게이트 판정에서

`POST /v1/plans/{id}/gate`, 시스템 재대조(`change_recheck`), WATCH 재시험 모두 같은 규칙입니다. 규칙 판 `gate-rules/2026-11-02.3`(두 출처 합치기, 9.5).

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
| `controls[].sources` | 두 출처가 같은 통제를 보고했을 때만: `[{"source": "executor" \| "gate", "src", "dst", "evidence_digest"}]`(출처 이름 순). 하나만 보고했으면 `null` |
| `controls[].missing_sources` | 한쪽이 측정했는데 UNKNOWN(또는 값 없음)을 낸 출처 이름. 없으면 `[]` |
| `controls[].reason` | 기존 값 + `sources_disagree`(두 출처가 PASS와 FAIL로 갈림) |
| `exceptions[]` | 이 판정에 적용한 예외. `{"exception_id", "control_id", "src", "dst", "evidence_digest", "expires_at", "approved_by", "compensating_control"}`. 콘솔은 "예외 승인"으로 승인자 · 기간 · 대체 통제와 함께 따로 보임 |
| `evidence_expires_at` | 게이트 보고서의 `expires_at`(증거 유효기간 끝). 게이트를 못 읽었으면 `null`. 지난 판정으로는 예외를 요청 · 승인하지 않음 |

### 9.5 게이트 서비스 쪽(`platform/gate`)

- `GET /v1/controls/report`의 `questions[]` 행에 `src_evidence_digest`, `dst_evidence_digest`를 더합니다. 값은 `"sha256:" + sha256(JSON(증거 객체))`. 증거 객체는 `src_evidence` · `dst_evidence` 그대로(`{"verdict", "reason", "evidence"}`), JSON은 키 정렬 · 구분자 `,` `:` · UTF-8. 시각은 증거에 없으므로 같은 관측이면 같은 값입니다.
- `GET /v1/controls`(4개 키 계약)는 바뀌지 않습니다.
- 예외는 게이트 서비스가 모릅니다. `EXCEPTION`은 API가 판정을 조립할 때만 나옵니다.
- API는 이제 `GET /v1/controls/report?source=<출발지 환경 이름>&target=<목적지 환경 이름>`을 읽습니다(전에는 인자 없는 `/v1/controls`라 게이트가 422로 답했습니다). 질문 행을 `{control_id, src, dst, evidence_digest, source: "gate"}`로 받고, Executor 행에는 `source: "executor"`를 붙입니다. 시간 제한 `BRAVO_GATE_TIMEOUT_S`(기본 60초). 게이트가 모르는 환경 이름이면 `gate_controls_unknown_environment`로 막습니다.
- **두 출처 합치기(팀장 결정 2026-11-02 23:4x KST).** `DB-ANON-READ`, `DB-AUTH`처럼 Executor와 게이트가 같은 통제 ID를 보고하면 원본(`src`) · 대상(`dst`)을 따로 합칩니다.
  - 두 값이 같으면 그 값을 씁니다.
  - 한쪽이 UNKNOWN(또는 값 없음)이고 다른 쪽이 PASS · FAIL이면 그 값을 쓰고, UNKNOWN을 낸 출처를 `missing_sources`에 적습니다.
  - PASS와 FAIL로 갈리면 그쪽은 UNKNOWN, 행 `reason`은 `sources_disagree`입니다. 그대로 막습니다(5a).
  - 두 출처의 값은 행의 `sources`에 다 남깁니다.
  - 행의 `evidence_digest`는 합친 `dst`를 실제로 측정한 출처의 digest입니다. digest는 게이트만 내므로 예외는 늘 게이트 증거에 묶입니다. 게이트가 그 `dst`를 측정하지 않았으면(UNKNOWN) `null`이고 예외를 걸 수 없습니다.
  - 같은 출처가 같은 ID를 두 번 내면(또는 출처 표시가 없으면) 모순으로 보고 UNKNOWN입니다.

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

- 표 `control_exceptions`: 9.1의 값(`compensating_control` 포함), `status`, `stale_reason`, `requested_by`, `requested_at`, `decided_by`, `decided_at`, `decision_note`, `updated_at`. RLS는 다른 표와 같습니다(`tenant_isolation` to `bravo_api` · `bravo_mcp`, 소유자 `owner_definer`). `bravo_api`는 읽기 · 쓰기, `bravo_mcp`는 읽기와 `stale` · `expired` 기록만(MCP `gate_run`이 같은 처리기를 씀). Executor · T31 워커 역할은 권한 없음.
- 트리거 `bravo_control_exceptions_guard`: 요청 때 요청자 = 세션 주체, 목적지 = 계획의 목적지, 판정 = 계획의 마지막 판정이고 그 행이 `dst = FAIL`, `src ∈ {PASS, FAIL}`, 같은 digest인지 확인. 결속 값은 바꿀 수 없음. 허용 전이만(9.2). 승인 · 거절은 결정자 = 세션 주체, approver 멤버십, 결정자 ≠ 요청자 ≠ 계획 작성자. 승인은 만료 전이고 마지막 판정의 그 행이 아직 같을 때만.
- 부분 유일 색인: 계획 · 통제마다 `requested` · `approved` 하나.
- `gate_verdicts.exceptions`(jsonb, 기본 `[]`): 그 판정에 적용한 예외. `gate_verdicts.evidence_expires_at`: 게이트 증거 유효기간 끝. 승인 때 트리거가 지나지 않았는지도 봅니다.

## 10. SHIFT: 트래픽 전환 10/50/100과 컷오버 시작(S3, FR-8, T17)

컷오버 승인 뒤 운영자가 콘솔에서 시작하면 계획은 SHIFT로 갑니다. Executor가 Edge 가중치를 10%, 50%, 100% 순서로 올리고, 단계마다 목적지가 받은 실제 채팅 쓰기의 ACK, 오류, 지연으로 판정합니다. 세 단계를 모두 통과하면 시스템이 같은 승인으로 DB 컷오버를 시작합니다. 한 단계라도 기준을 넘으면 목적지를 바로 0%로 돌리고 AWAIT_APPROVAL로 돌아갑니다(S3 예외 1a). 이 절은 2026-11-03 리뷰 24(B02, B14, B04)를 닫는 계약이고, 실측은 10.6절에 붙입니다.

### 10.1 상태와 전이

| 지금 상태 | 계기(trigger) | 다음 상태 | 누가, 언제 |
|---|---|---|---|
| AWAIT_APPROVAL | `shift_started` | SHIFT | 콘솔 운영자. 만료 전의 컷오버 승인이 있어야 함 |
| SHIFT | `shift_done` | CUTOVER | 시스템. shift 실행이 DONE(세 단계 통과, 목적지 100%) |
| SHIFT | `shift_reverted` | AWAIT_APPROVAL | 시스템. shift 실행이 ABORT_PRE_WRITE(판정 실패로 0% 복귀, 또는 가중치 변경 전 거부). 새 1회용 컷오버 승인 요청을 냄 |
| SHIFT | `run_safe_hold` | SAFE_HOLD | 시스템. 가중치 변경을 보냈는데 반영을 확인하지 못함 |
| CUTOVER | 이전과 같음 | WATCH, ABORT_PRE_WRITE(→ AWAIT_APPROVAL), SAFE_HOLD | 시스템 |

- AWAIT_APPROVAL에서 CUTOVER로 바로 가는 전이는 없앴습니다. DB 컷오버는 SHIFT를 거쳐야만 시작합니다.
- 순서는 스펙 §8.2 상태 표를 따릅니다. AWAIT_APPROVAL의 다음 조건이 "승인 값이 현재 값과 같고 만료 전"이고, SHIFT의 다음 조건이 "단계마다 오류율 · p95 기준 이하"입니다. S3 기본 흐름 글은 전환 뒤에 승인하는 순서로도 읽혀서, 이 절에서 승인 → 콘솔 시작 → SHIFT → CUTOVER로 고정합니다.
- 컷오버 승인 하나가 SHIFT와 CUTOVER를 함께 덮습니다. API는 SHIFT 시작 때 승인을 소비합니다(`consumed_at`, `consumed_by` = 시작한 운영자, `run_idem_key` = `cutover-<approval_id>`).
- Executor 멱등 키는 shift가 `shift-<approval_id>`, 컷오버가 `cutover-<approval_id>`입니다.
- 컷오버 실행이 쓰기 전에 실패하면(ABORT_PRE_WRITE) 가중치는 목적지 100%에 그대로 둡니다. 그 상태에서도 목적지 앱은 목적지 PgBouncer를 거쳐 출발지 DB에 씁니다. 다음 승인으로 SHIFT를 다시 시작하면 10%부터 다시 걷습니다.
- 역방향 복제 슬롯은 미리 만들지 않습니다. 컷오버 6a 단계에서 양쪽 쓰기가 막히고 barrier 비교가 맞은 뒤, Executor가 목적지에 슬롯 `bravo_rev_sub`를 만들고(그때의 LSN을 기록) 출발지의 꺼진 역방향 구독에 붙인 뒤 켭니다. 미리 만든 슬롯은 구독이 꺼진 동안 목적지 WAL을 붙잡습니다(2026-11-03 운영 갈래 실측: 분당 약 3.6 MB, `max_slot_wal_keep_size` 2 GB에 약 7시간). 컷오버 전 점검은 슬롯이 없어야 통과합니다(`REVERSE_SLOT_PRESENT`). 쓰기 전에 실패하면 슬롯을 떼고 지웁니다. `repl_setup setup`은 역방향 구독을 `slot_name = NONE`으로 만들고, `repl_setup drop-rev-slot`은 컷오버 전의 남은 슬롯을 지웁니다(`reset-demo.sh`가 부름).

**Executor의 승인 확인(감사 A2, 5절에 더함).** shift와 컷오버 모두 아래를 모두 만족해야 실행합니다. 어긋나면 403과 코드로 거부하고 실행 행을 만들지 않습니다.

| 확인 | 거부 코드 |
|---|---|
| 승인 행이 있고 같은 계획, `stage = cutover`(되돌리기는 `failback`) | `APPROVAL_NOT_FOUND`, `APPROVAL_PLAN_MISMATCH`, `APPROVAL_STAGE_MISMATCH` |
| 승인자 ≠ 요청자, 만료 전 | `APPROVER_IS_REQUESTER`, `APPROVAL_EXPIRED` |
| API가 콘솔 시작으로 소비함(`consumed_at` 있음). 이것이 "운영자 콘솔 시작"의 증거 | `APPROVAL_NOT_STARTED` |
| 시작한 사람 ≠ 승인자(`consumed_by <> approver_id`) | `STARTER_IS_APPROVER` |
| 같은 종류의 다른 실행이 이 승인을 쓰지 않음(shift 하나, 컷오버 하나) | `APPROVAL_ALREADY_USED` |
| 컷오버만: 같은 승인의 shift 실행이 DONE | `SHIFT_NOT_DONE` |

승인 행은 API만 만듭니다(콘솔 approver). Executor와 스크립트는 승인 행을 만들지 않습니다.

### 10.2 콘솔 API

| 요청 | 응답 |
|---|---|
| `POST /v1/plans/{id}/shift` (콘솔 operator) `{"stage": "cutover", "write_pause_budget_ms": 5000}` + `Idempotency-Key` | 202 `{"plan_id", "stage": "cutover", "approval_id", "epoch", "run_id", "run_kind": "shift", "state": "SHIFT", "reason": null}`. `run_id`는 shift 실행. 오류: 409 `state_mismatch` · `run_active` · `approval_missing` · `approval_stale`, 403 `insufficient_scope`(approver, MCP) |
| `POST /v1/plans/{id}/shift` (콘솔 operator) `{"stage": "failback"}` | 이전과 같음(WATCH → FAILBACK). 응답에 `run_kind: "failback"`. 앞단 이동은 10.7절 |
| `GET /v1/plans/{id}` | 더한 키: `shift`(아래), `run.write_pause_budget_ms`(시작 때 받은 값), `run.stages[].front`(10.4절). `run.kind`는 shift · cutover · failback 중 하나 |
| `GET /v1/plans/{id}/runs/{run_id}/replay` | 12절 |

`shift`는 이 계획의 마지막 shift 실행입니다. 없으면 `null`입니다.

```json
{"run_id": "<uuid>", "status": "RUNNING", "target_env": "dst", "current_weight": 50,
 "started_at": "…", "ended_at": null,
 "thresholds": {"min_target_samples": 100, "max_error_rate": 0.01, "max_p95_ms": 800.0, "min_hold_s": 5.0,
                "max_hold_s": 40.0, "min_target_share": 0.25, "write_rate": 60.0, "target_write_rate": 20.0,
                "write_keepalive": false, "revert": true},
 "steps": [{"weight": 10, "healthy": true, "target_samples": 104, "target_errors": 0, "target_error_rate": 0.0,
            "target_p95_ms": 212.4, "samples": 1003, "errors": 0, "error_rate": 0.0, "p95_ms": 88.1,
            "by_env": {"aws": 899, "onprem": 104}, "hold_ms": 16800, "step_ms": 17400, "reasons": []}],
 "reverted": null}
```

- `current_weight`: 마지막 `front.set_<w>` 단계의 값. 판정이 끝나기 전에도 화면이 지금 몇 %인지 보이게 합니다.
- `reverted`: 1a로 돌렸으면 `{"at_weight": 10, "reasons": ["TARGET_ERROR_RATE(0.0472>0.01)"]}`.

### 10.3 이벤트 순서

| 순서 | kind | data |
|---|---|---|
| 1 | `state.changed` | AWAIT_APPROVAL → SHIFT, `trigger` `shift_started`, `approval_id`, `epoch`, `stage` cutover, `write_pause_budget_ms` |
| 2 | `run.stage` | `front.preflight`(모드, 지금 가중치, 목적지 터널 연결 수) |
| 3 | `run.stage` | 단계마다 두 번. `front.set_<w>`(가중치 반영 확인 직후, `front` {`weight`, `pool_weights`}), `front.step_<w>`(보유가 끝나 판정한 뒤, `front` {`weight`, `judgement`}). 판정 실패면 `result` FAILED |
| 3a | `run.stage` | 1a면 `front.revert_0`(`front` {`at_weight`, `reasons`, `pool_weights`}) |
| 4 | `run.stage` | `front.summary` |
| 5 | `run.done` | `kind` shift, `status`, `write_pause_ms` null |
| 6 | `state.changed` | SHIFT → CUTOVER(`shift_done`). 이어서 컷오버 실행의 `run.stage`*, `run.done`, `receipt.saved`, `state.changed` CUTOVER → WATCH, `approval.requested`(failback) |
| 6a | `state.changed` | SHIFT → AWAIT_APPROVAL(`shift_reverted`, `reason` `shift_reverted:<첫 이유>`), 이어서 `approval.requested`(cutover) |

### 10.4 단계 판정의 입력과 규칙

판정 입력은 대표 이름(`b-free-chat.mangowhois.ai`)으로 들어온 실제 채팅 쓰기입니다. Executor가 보유 시간 동안 `POST /api/rooms/shift-probe/messages`를 일정 간격으로 보냅니다. 요청마다 새 `Idempotency-Key`를 쓰고, 본문은 `{"author": "bravo-shift", "body": "shift sample <run_id> <n>"}`입니다. 이 쓰기는 실제 DB 행이 됩니다. 목적지 앱이 받은 쓰기는 목적지 PgBouncer와 링크를 거쳐 출발지 DB에 씁니다(스펙 "전환 전 쓰기 경로").

| 표본 판정 | 규칙 |
|---|---|
| 성공(ACK) | HTTP 200 또는 201, JSON `message.id`가 정수, `X-Served-By` 헤더 있음 |
| 오류 | 그 밖의 모두. 5xx(장애 주입 `X-Bravo-Fault`, 앱의 503 `write_timeout` 포함), 4xx, 503 `blocked`, 10초 시간 초과, 연결 오류. 다시 보내지 않음 |
| 어느 환경 | `X-Served-By`(`aws` = 출발지, `onprem` = 목적지). 시간 제한 10초는 앱의 쓰기 기한(`WRITE_WAIT_S` 8초)보다 길어서, 느린 쓰기도 앱의 503과 헤더로 돌아옴. 헤더 없는 오류(Cloudflare 52x, 연결 오류)만 남고 이것은 목적지 오류로 셈(보수적) |
| 연결 | 쓰기마다 새 연결(`BRAVO_FRONT_WRITE_KEEPALIVE=0`)과 다른 쿼리(`?_s=<run>-<n>`). 연결을 유지한 POST는 처음 간 pool에 붙었습니다(10.6절). 지연은 요청을 보낸 때부터 응답 헤더까지라서 표본기 자신의 TCP · TLS 준비는 넣지 않습니다 |

| 값 | 기본값(환경 변수) | 이유 |
|---|---|---|
| 표본 쓰기 속도 | 단계마다 min(초당 60건, 초당 20건 ÷ 가중치)(`BRAVO_FRONT_WRITE_RATE`, `BRAVO_FRONT_TARGET_WRITE_RATE`). 10% 60건, 50% 40건, 100% 20건 | 10% 단계에서 목적지 100건을 모으고, 50 · 100% 단계는 목적지에 초당 20건만 보냄(목적지 쓰기는 링크를 건넘) |
| 방 | `shift-probe`(`BRAVO_FRONT_WRITE_ROOM`) | 심사위원 방(`consult-1`)과 섞이지 않음. 화면과 기록에서 표본임이 보임 |
| 목적지 최소 표본 | 100건(`BRAVO_FRONT_MIN_TARGET_SAMPLES`) | 100건이면 오류 1건이 1.0%로 기준 안이고, 2건이 2.0%로 기준을 넘음. 100건보다 적으면 오류 1건만으로 1%를 넘어 우연이 판정을 정함 |
| 최소 보유 | 5초(`BRAVO_FRONT_MIN_HOLD_S`) | Cloudflare LB 반영 지연 동안의 표본도 판정에 넣음 |
| 최대 보유 | 40초(`BRAVO_FRONT_MAX_HOLD_S`) | 그 안에 목적지 100건을 못 모으면 `INSUFFICIENT_SAMPLES`로 0% 복귀 |
| 목적지 오류율 | 1% 이하(`BRAVO_FRONT_MAX_ERROR_RATE`) | FR-8 |
| 목적지 p95 | 800 ms 이하(`BRAVO_FRONT_MAX_P95_MS`) | 목적지 앱은 링크 너머 출발지 DB에 씀. 10.6절 실측으로 확인 |
| 목적지 몫 | 계획 몫의 1/4 이상(`BRAVO_FRONT_MIN_TARGET_SHARE` 0.25) | 10% 단계면 표본의 2.5% 이상. Cloudflare pool 모니터가 목적지를 빼고 출발지로 넘기면 오류는 0이어도 목적지를 검증하지 못함(2026-11-02 가짜 DONE) |

보유는 가중치 반영을 확인한 때부터 셉니다. 목적지 표본이 100건 이상이고 5초가 지나면 끝납니다. 출발지의 오류율과 p95는 `samples`, `error_rate`, `p95_ms`로 함께 보이지만 판정에는 쓰지 않습니다.

`run.stages[].front.judgement`는 10.2절 `shift.steps[]`의 한 항목과 같은 키이고, `source`(`loadgen` · `probe`), `load_run`, `write_rate`를 더합니다.

**판정 기준값은 실행이 들고 다닙니다.** Executor는 `front.preflight`, `front.set_<w>`, `front.step_<w>`, `front.revert_0`의 `front.thresholds`에 그 실행이 실제로 쓴 값(`min_target_samples`, `max_error_rate`, `max_p95_ms`, `min_hold_s`, `max_hold_s`, `min_target_share`, `write_rate`, `target_write_rate`, `write_keepalive`, `revert`)을 적습니다. API는 같은 값을 `shift.thresholds`로 냅니다. 콘솔은 이 값으로 기준선을 그립니다. 환경 변수를 바꿔도 화면과 판정이 어긋나지 않습니다.

**판정 입력이 아닌 것.** `GET /api/config` 프로브는 `GET /v1/front` 상태 표시와 CNAME 모드 전파 시간에만 씁니다. 앱 쪽 카운터(`chat_writes_total`)는 대조 증거로만 씁니다.

**판정 원천: 사용자 부하가 먼저, 표본기는 대체(2026-11-03 코디네이터 결정).** 단계를 시작할 때 부하 실행이 돌고 있으면(플랫폼 Prometheus의 `bravo_loadgen_attempts_total` 마지막 표본이 15초 안) 그 부하로 판정합니다. 계획의 `bravo_plan_id`가 붙은 실행을 먼저 찾고, 없으면 아무 실행(`run_id!=""`)을 씁니다. 개수는 무게 반영 시각과 지금의 순간값 차이(쓰기만, `served_by`별)이고, p95는 목적지 쓰기 성공 시도의 히스토그램입니다. 지표가 5초마다 오므로 이 원천의 보유는 15초 이상입니다. 부하 실행이 없으면 Executor 표본기를 씁니다. 그래서 표본기 쓰기가 사용자 부하에 더해지지 않습니다. 어느 원천을 썼는지는 단계마다 `judgement.source`에 남습니다. Prometheus를 읽지 못하면 그 단계는 `LOADGEN_METRICS_UNAVAILABLE`로 0% 복귀합니다. 지표 계약은 `tools/loadgen/README.md` "지표 계약"입니다.

### 10.5 기준을 넘으면(1a)

Executor는 그 단계의 판정이 끝난 즉시 목적지 pool 가중치를 0, 출발지를 1로 바꾸고 반영을 확인합니다(`front.revert_0`). 실행 상태는 ABORT_PRE_WRITE입니다. DB는 건드리지 않았으므로 쓰기 전 실패와 같습니다. API는 SHIFT → AWAIT_APPROVAL로 옮기고 새 컷오버 승인 요청을 냅니다. 소비한 승인은 다시 쓸 수 없습니다. 0% 복귀 자체를 확인하지 못하면 SAFE_HOLD입니다.

### 10.6 실측(2026-11-03 02:51–03:03 KST)

Cloudflare LB `b-free-chat.mangowhois.ai`에서 Executor 도구 경로(10.9절)로 쟀습니다. 이미지 핀을 아직 올리지 않아서, 커밋 `9253b8b`의 `bravo_executor`(와 이 절의 표본 설정)를 executor-0의 `/tmp`에 복사해 그 Pod 안에서 돌렸습니다. 실행 · 단계 · 효과 행은 플랫폼 원장에 그대로 남았습니다. 원기록은 `docs/evidence/shift/lb-oneway-20261103T025135/`입니다.

| 시각 | 실행 | 표본 | 결과 | 편도(실행 시작부터 끝까지) | 10% 단계 목적지 몫 |
|---|---|---|---|---|---|
| 02:57:07 | `c7f71737` | 실험: `GET /api/status`(DB 쓰기 없음), 연결마다 새로 | DONE | 30.4초 | 109/782(13.9%), 보유 13.5초 |
| 03:01:18 | `910e45fb` | 실험: `GET /api/status`, 연결 유지 | DONE | 37.2초 | 101/1215(8.3%), 보유 20.8초 |
| 02:51:41 | `6318c256` | 실제 쓰기, 연결 유지 | 10%에서 0% 복귀 | 34.9초 | 10/1801(0.6%), 목적지 p95 2,639 ms |
| 02:53:40 | `9ba7bf66` | 실제 쓰기, 연결마다 새로 | 10%에서 0% 복귀 | 38.4초 | 40/1801(2.2%), 그중 오류 29 |
| 03:02:40 | `59fbeecb` | 실제 쓰기, 최종 설정(연결 유지, 쓰기마다 다른 쿼리, 10초) | 10%에서 0% 복귀 | 33.8초 | 26/1801(1.4%), 모두 오류 |

- **보유 규칙은 60초 안에 듭니다(측정함).** 목적지가 빨리 답하면 편도가 30.4초와 37.2초였습니다. 10% 단계 보유가 13.5초와 20.8초, 50%가 6.8초와 5.3초, 100%가 6.0초와 5.1초입니다. 이전 기준(단계마다 60초 보유)의 편도는 184.8초였습니다.
- **실제 쓰기로는 목적지 전환이 통과하지 못했습니다(FAIL, 측정함).** 목적지 앱의 쓰기 경로(목적지 PgBouncer, 링크, 출발지 DB)가 느립니다. LB를 거치지 않고 `b-free-chat-dst`에 차례로 보낸 쓰기 8건은 02:56 KST에 2,095–2,454 ms, 다른 부하가 없던 03:10 KST에 1,710–2,633 ms였습니다(모두 201). 같은 때 출발지는 98–415 ms였습니다. 03:02 실행은 다른 갈래의 부하 실행과 겹쳤습니다. 부하 생성기가 03:01:23부터 60초 동안 LB를 거치지 않고 `b-free-chat-dst`에 초당 50건을 써서 목적지 쓰기가 8초 시간 초과로 쌓였습니다(앱 카운터 `timeout` 17에서 19,006으로). 그래서 그 실행의 오류 수는 판단에 쓰지 않습니다. 02:56부터 300초 동안은 같은 갈래가 `b-free-chat-src`에 썼습니다. 단계 판정은 매번 1a대로 목적지를 0%로 돌렸습니다. 링크 쪽 지연을 고치기 전에는 S3 SHIFT가 10%에서 멈춥니다.
- **원인 미확인(UNKNOWN).** 목적지 쓰기가 느릴 때 Cloudflare는 10% 가중치에서도 쓰기(POST)를 목적지로 0.6–2.2%만 보냈습니다. 앱 쪽 카운터(`chat_writes_total`)도 같은 수를 보였습니다. 같은 pool로 가는 `GET /api/status`는 가중치를 따랐습니다(8.3–13.9%). 연결 유지 여부와 쿼리 차이로는 바뀌지 않았습니다.
- p95 기준 800 ms는 그대로 둡니다. 스펙은 이 기준을 실측 지연(T8)으로 정하라고 합니다. 지금 잰 값(쓰기 한 건 2.1–2.5초)은 고장 난 경로의 값이므로 기준으로 삼지 않습니다. 링크를 고친 뒤 다시 재고 `BRAVO_FRONT_MAX_P95_MS`를 정합니다.
- 측정이 끝난 뒤 LB는 출발지 100%입니다(03:03 KST `front-after-final.json`).

**2차(03:15–03:53 KST): 원인을 재고 고쳤습니다.** 원기록은 `docs/evidence/shift/write-path-20261103/`, `lb-real-writes-20261103T033356/`, `pgbouncer-durable-target-20261103/`입니다.

1. **목적지 쓰기 지연은 왕복 횟수 때문이었습니다(측정함).** 온프렘 앱 Pod에서 PgBouncer와 링크를 거친 `SELECT 1`은 338–341 ms였고, PgBouncer 연결은 열 때 11 ms였습니다. 서버 연결은 이미 재사용되고 있었습니다. 쓰기 한 건은 BEGIN, set_config, 쓰기 차단 확인, INSERT, SELECT, COMMIT의 6왕복으로 2,040 ms였습니다. 앱 `a4636ac`가 같은 문장을 한 번의 simple-query로 보내 1왕복 340 ms가 됐습니다(verify-full 유지). 플랫폼에서 대표 이름 없이 목적지에 차례로 보낸 쓰기는 p50 698 ms, p95 705 ms가 됐습니다(이전 2.1–2.6초).
2. **링크 한 왕복의 바닥은 약 290–340 ms입니다(측정함).** 플랫폼에서 같은 Cloudflare 링크로 두 DB에 `SELECT 1`은 8.6 ms와 10.2 ms였습니다. 온프렘의 `cloudflared access tcp`가 닿는 `t1-src-db.mangowhois.ai`는 이 노트북 망(KT)에서 Cloudflare `colo=DFW`로 갑니다(`www.cloudflare.com`은 `ICN`). 그래서 온프렘에서 출발지 DB로 가는 한 왕복이 미국을 돌아옵니다. 동시에 12개를 보내면 한 왕복이 400–580 ms로 늘었습니다.
3. **PgBouncer 풀(측정함).** 목적지 풀 10개는 이미 다 열려 있었습니다. 부하 갈래 측정으로 초당 약 16건이 한계라서 `default_pool_size` 30, `min_pool_size` 10으로 올렸습니다(온프렘 · 출발지, 03:46 KST 적용). 고친 뒤 차례 쓰기 20건은 p50 598 ms, p95 859 ms였습니다.
4. **POST 몫(측정함).** 50% 단계에서 같은 표본기로 섞어 보냈습니다. 연결을 유지한 POST는 150건 중 9건, 쓰기마다 새 연결인 POST는 57건, GET은 연결 방식과 상관없이 60–62건이 목적지로 갔습니다. 표본기는 쓰기마다 새 연결을 씁니다. 그 뒤 10% 단계 실행에서 목적지 몫은 108/1636(6.6%)이었고 앱 카운터도 같았습니다(목적지 +108, 출발지 +1,528).
5. **아직 통과하지 못했습니다(FAIL).** 03:36 실행은 목적지 표본 108건, 오류 0건이었지만 목적지 p95 956 ms(표본기의 TLS 준비 포함)로 0% 복귀했습니다. 03:49와 03:52 실행은 출발지 앱 쪽 문제로 판정하지 못했습니다. 출발지 EKS의 앱 복제본 중 노드 `ip-10-10-11-107`의 것에 Gateway(Envoy, 노드 `ip-10-10-11-11`)가 닿지 못해 새 요청의 절반이 Cloudflare 503(`upstream connect error … connection timeout`, `X-Served-By` 없음)이었습니다. 이 오류는 보수적 규칙대로 목적지 오류로 셉니다. 03:48 무렵 앱 0.3.3 배포 뒤 생긴 일이고 Pod를 다시 만들어도 같은 노드에 뜨면 그대로입니다.
6. **결정 요청: 목적지 p95 기준.** 컷오버 전 목적지 쓰기는 링크를 한 번 건너므로 바닥이 한 왕복(290–340 ms)이고, 풀 대기와 동시 접속에서 600–900 ms가 나옵니다. 기준 800 ms는 그대로 뒀습니다. 선택지는 둘입니다. 첫째, 컷오버 전 SHIFT 판정만 1,500 ms로 둡니다(스펙 "가중치 단계의 판정 기준은 실측 지연(T8)으로 정한다"). 둘째, 온프렘 링크 클라이언트가 미국 POP로 가지 않게 경로를 바꿉니다. 지금 값을 슬쩍 바꾸지 않았습니다.
7. **PgBouncer 활성 대상이 재시작에도 남습니다(측정함).** Executor가 대상을 바꿀 때 ConfigMap `chat-pgbouncer-target`(`target`)도 고칩니다. init 컨테이너는 그 값을 읽습니다. 온프렘에서 Executor 코드와 권한으로 dst로 바꾸고 재시작하니 `target-dst.ini`가 선택됐고, src로 되돌리고 재시작하니 `target-src.ini`였습니다(03:47 KST). Executor 권한은 `chat` 네임스페이스에서 그 ConfigMap 이름 하나에 대한 get · patch뿐입니다. 컷오버 전 점검은 이 값이 출발지가 아니면 거부합니다(`POOL_<이름>_DURABLE_TARGET_NOT_ORIGIN`).

LB는 측정이 끝난 뒤 출발지 100%입니다(03:52 KST).

### 10.7 S4 되돌리기의 앞단

되돌리기 실행(`POST /v1/failback`)은 출발지 쓰기를 확인한 뒤 마지막 단계 `failback.8_front_to_src`에서 Edge를 출발지 100%, 목적지 0%로 한 번에 옮깁니다(스펙 S4 기본 흐름 4). 단계를 나누지 않고 0% 복귀도 하지 않습니다. 고장 난 쪽이 목적지이기 때문입니다. 반영 확인 뒤 5초 동안 같은 표본 쓰기를 보내 출발지 판정을 기록합니다. 앞단 이동이 실패해도 DB 되돌리기는 끝났으므로 실행은 DONE이고 그 단계만 FAILED입니다. 이때 계획은 DONE_ROLLED_BACK이 되고 `state_reason`이 `front_not_moved`입니다. 운영자는 `deploy/cloud/front_shift.sh src`로 앞단을 옮깁니다.

### 10.8 장애 주입(S4의 계기)

`deploy/cloud/t25.sh fault [비율]`은 목적지 앱 Pod마다 관리 포트 8081의 `PUT /admin/fault {"http_5xx_rate": 비율}`만 부릅니다(기본 0.3, `fault.sh`). 이어서 대표 이름으로 쓰기 20건을 보내 오류가 보이는지 기록합니다. 컷오버와 되돌리기는 부르지 않습니다. `t25.sh fault-clear`가 비율을 0으로 돌립니다. 되돌리기는 콘솔에서만 시작합니다. 승인자가 되돌리기 요청을 승인하고, 운영자가 시작합니다.

### 10.9 Executor 직접 경로(도구)

`POST /v1/front/shift`에 `plan_id`가 없으면 도구 실행입니다. T17 증거와 `deploy/cloud/front_shift.sh`가 이 경로를 씁니다. 승인을 보지 않고, 판정과 0% 복귀는 같습니다. `plan_id`를 주면 `approval_id`와 `stage`도 있어야 하고 10.1절의 확인을 모두 거칩니다. 본문: `{"target_env": "dst", "steps": [10, 50, 100], "plan_id", "epoch", "approval_id", "stage": "cutover"}`.

## 11. 무결성 영수증: `verdict_parts`, hash_rule v1, 검사 범위

영수증은 DB 표, 시퀀스, 첨부 객체를 따로 판정하고 셋을 합쳐 하나의 판정을 냅니다. 각 판정이 무엇을 검사했는지는 `scope`에 적습니다. 영수증은 목적지 쓰기를 열기 전에 저장합니다(S3-AC2).

### 11.1 판정

| `verdict` | 조건 |
|---|---|
| `MATCH` | `verdict_parts`의 `tables`, `sequences`, `objects`가 모두 MATCH |
| `MATCH_PARTIAL` | `tables`와 `sequences`는 MATCH이고 `objects`가 `NOT_CHECKED`(객체 저장소가 설정되지 않음). DB는 맞았지만 첨부는 검사하지 않았다는 뜻 |
| `MISMATCH` | 하나라도 MISMATCH. 컷오버는 확정 전에 취소(ABORT_PRE_WRITE) |

```json
{"verdict": "MATCH",
 "verdict_parts": {"tables": "MATCH", "sequences": "MATCH", "objects": "MATCH"},
 "scope": {"hash_rule": "v1.1",
           "tables": ["messages", "bravo_barrier", "attachments"],
           "sequences": ["messages_id_seq", "bravo_barrier_id_seq"],
           "objects": {"checked": "target_head_after_final_copy", "referenced": 3,
                       "reference_set_sha256": "…", "store_from": "…", "store_to": "…"}},
 "tables": [{"name", "rows": {"src", "dst"}, "pk_hash": {…}, "content_hash": {…}, "match"}],
 "sequences": [{"name", "origin_last", "target_before", "target_set", "match"}],
 "objects": {"verdict", "referenced", "verified_on_target", "missing", "size_mismatch", "reference_set_sha256", …}}
```

`objects.checked`는 `target_head_after_final_copy`(검사함) 또는 `not_configured`(검사하지 않음)입니다.

### 11.2 hash_rule v1

스펙 hash_rule v1은 "PK 정렬, NULL 처리, 타입 직렬화 고정"입니다. 영수증의 `hash_rule: "v1.1"`은 v1에 uuid 기본 키(첨부 표)의 정렬만 더한 것입니다. 정수 기본 키 표의 해시는 v1과 바이트까지 같습니다. 규칙의 정본은 `platform/executor/src/bravo_executor/hashing.py` 머리 주석이고, 고정 값 시험은 `platform/executor/tests/test_hashing.py`입니다. 규칙을 바꾸면 이름을 올립니다(v1.2).

| 항목 | 규칙 |
|---|---|
| 행 순서 | 기본 키 오름차순. 정수는 수 순서, uuid는 소문자 표준 문자열의 바이트 순서(`ORDER BY id::text COLLATE "C"`). 앞 키보다 크지 않은 키가 오면 해시하지 않고 오류 |
| `rows` | 행 수 |
| `pk_hash` | 행마다 `<pk>\n`의 SHA-256. 정수는 10진수, uuid는 8-4-4-4-12 소문자 |
| `content_hash` | 행마다 선언 순서의 열을 `\|`로 잇고 `\n`으로 끝낸 줄의 SHA-256. `messages`는 `id\|room\|author\|body\|idem_key\|created_at` |
| NULL | `\N` |
| 문자열 | UTF-8. `\` → `\\`, `\|` → `\\|`, LF → `\n`, CR → `\r` |
| timestamptz | UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`(소수 6자리) |
| bool, uuid, 정수 | `t`·`f`, 소문자 표준형, 10진수 |
| 시퀀스 | 마지막으로 낸 값(`is_called`면 `last_value`, 아니면 `last_value - 1`). 목적지 값이 출발지 값 이상이면 MATCH |

### 11.3 첨부 대조: 최종 참조 집합

컷오버 1b 단계에서 출발지의 첨부 쓰기를 얼린 뒤(D-04) 다음을 합니다. 얼린 뒤에는 첨부 행이 바뀌지 않으므로 이때의 참조 집합이 최종입니다.

1. 남은 객체를 목적지 저장소로 복사합니다(`attachment_copies` 원장).
2. 출발지 DB의 참조 집합(`attachments`에서 출발지 저장소의 `object_key`, `sha256`, `size_bytes`)을 읽어 `reference_set_sha256`(키 순서로 `<object_key> <sha256> <size>\n`을 이은 SHA-256)을 냅니다.
3. 참조 키마다 목적지 저장소에 HEAD를 보내 있음과 크기를 확인합니다. 객체 메타데이터에 `sha256`이 있으면 그 값도 봅니다.

5단계(양쪽 쓰기 정지 중 비교)에서 참조 집합 해시를 다시 계산합니다. 1b의 값과 같고 3에서 빠진 객체가 없을 때만 `objects`가 MATCH입니다. 그래서 첨부 대조는 확정 지점(6b) 전에 끝납니다. HEAD는 쓰기 정지 전(1b)에 하므로 쓰기 정지 시간에 들어가지 않습니다.

### 11.4 지난 영수증의 검사 범위

`scope`가 없는 영수증(2026-11-03 이전 실행)에는 API가 `verification_scope`를 붙여 냅니다. 영수증 본문은 바꾸지 않습니다.

| 영수증 | `verification_scope` |
|---|---|
| `scope` 있음 | `{"source": "receipt", …scope}` |
| `verdict_parts`만 있음 | `{"source": "derived", "tables": […이름], "sequences": […이름], "objects": verdict_parts.objects}` |
| 둘 다 없음 | `{"source": "derived", "tables": […이름], "sequences": […이름], "objects": "not_in_receipt"}`. 그때는 첨부 객체를 검사하지 않았다는 뜻 |

## 12. 기록 재생(`mode: recorded`)

대체 장면을 보일 때 콘솔은 지난 실행을 기록 재생으로만 보입니다. 재생 응답의 모든 이벤트가 `mode: "recorded"`와 원래 시각을 가지므로 화면이 실시간처럼 보이지 않습니다. 재생은 아무것도 쓰지 않습니다.

`GET /v1/plans/{id}/runs/{run_id}/replay` (viewer 이상, 콘솔):

```json
{"mode": "recorded", "plan_id": "<uuid>", "run_id": "<uuid>", "kind": "shift",
 "status": "DONE", "recorded_at": "<실행 시작 시각>", "ended_at": "…", "generated_at": "<응답 시각>",
 "events": [{"id": "r1", "plan_id": "…", "state": null, "kind": "run.stage", "at": "<원래 시각>",
             "mode": "recorded", "run_id": "…", "actor": {"kind": "system", "principal": null},
             "offset_ms": 0, "data": {…1절과 같은 run.stage data}}]}
```

- `events`: 실행의 `run_stages` 행마다 `run.stage` 하나, 끝났으면 `run.done` 하나, 영수증이 있으면 `receipt.saved` 하나. `data`는 1절의 실시간 이벤트와 같은 모양입니다.
- `offset_ms`: 실행 시작부터의 경과. 콘솔은 이 값으로 같은 간격으로 재생할 수 있습니다.
- 콘솔은 재생하는 동안 화면 위에 "기록 재생"과 `run_id`, `recorded_at`을 늘 보입니다.
- 오류: 404 `run_not_found`(그 계획의 실행이 아님). 실행 중(RUNNING)인 실행은 409 `run_active`입니다.

## 스펙 밖

`GET /v1/plans`, `GET /v1/environments`, `POST /v1/plans/{id}/changes`, `GET /v1/cost`(T1 "component 태그별 비용 조회"를 API로 낸 것), JIT 역할 파일, `envs/<이름>/…` 비밀 참조는 스펙 §8.9에 없습니다. 2026-11-02 코디네이터 지시(초보 고객이 첫 화면에서 모든 것을 보고 시작)로 넣었습니다. 회의에서 `docs/spec/DEVIATIONS.md`에 올릴지 정합니다.
