# 가짜 · 스텁 감사 (2026-11-02 20:19–20:37 KST)

읽기 전용 감사 결과입니다. 번호는 담당에게 넘길 때 쓰는 ID입니다. 상태 칸은 고친 갈래가 갱신합니다.

| # | 등급 | 위치 | 문제 | 담당 | 상태 |
|---|---|---|---|---|---|
| A1 | 치명 | `deploy/pilot/README.md:39,40,42` | 증거 파일 없는 수치(18:52 107 ms, 18:58 97 ms). 19:26 불일치 42 ms는 run.json과 모순(`write_pause_ms: null`, 단계 합 66 ms). 실제 DONE 기록: 76, 80, 87, 92, 94, 101, 105, 139 ms | 문서 | 고침(T29, 문서 갈래 21:0x). 실행 ID 표로 바꾸고 "파일럿(노트북) 기록"으로 표시. 원본 사본 `docs/evidence/pilot-notebook/` |
| A2 | 치명 | `platform/executor/src/bravo_executor/app.py:31`, `service.py:175`, `cutover.py:61,152` | Executor가 아무 `approval_id`나 받음(최소 길이만 확인). 승인 표를 확인하지 않음. 스크립트가 가짜 ID(`smoke-approval`, `e1`)에 기댐 | 데이터 · 컷오버 | 고침(Executor 쪽, 21:0x): `approvals.py`가 플랫폼 DB `approvals`를 읽어 계획 · 단계 · 요청자≠승인자 · 만료 · 재사용을 확인, 아니면 403. 표가 없거나 못 읽으면 거부(APPROVAL_SOURCE_UNAVAILABLE). 시험 `tests/test_approvals.py`. 스크립트는 실제 승인 ID를 env로만 받음. **남음**: platform/api가 `bravo_executor`에 `approvals` SELECT · 정책을 주고 `stage`에 `failback`을 더해야 함. executor-0은 아직 예전 이미지(검증 없음)라 새 이미지 `bravo-platform/executor@sha256:32c2b92b…`로 바꿔야 적용됨 / **API 쪽 고침(21:4x)**: 마이그레이션 0004가 `bravo_executor`에 `approvals` SELECT와 RLS 정책 `executor_cell`(읽기만)을 줌. `stage`는 `change` · `cutover` · `failback`. 계약은 `docs/architecture/15-r2-contracts.md` 5절. 시험 `platform/api/tests/test_changes.py::test_executor_role_reads_approvals_for_its_a2_check`. 서명된 승인 토큰은 만들지 않음. 남음: 0003–0006이 플랫폼 DB에 적용돼야 함(EKS 판은 0001–0002만 적용됨) / **닫힘(23:3x)**: 플랫폼 RKE2 DB에 0001–0007 적용 확인(`schema_migrations`). executor-0은 A2 검사가 든 이미지(`kustomization.yaml` 고정 digest)로 동작 |
| A3 | 치명 | `platform/api/src/bravo_api/rest.py:159-171`, `devseed.py`, `console/src/state.tsx:66` | 개발용 IdP가 스텁. 인증 없이 operator · approver JWT를 주는 `/v1/dev/tokens`, 존재하지 않는 발급자 · 사용자, 콘솔 자동 로그인 | 플랫폼 API | 고침(플랫폼 API, 21:3x): Access JWKS를 certs URL에서 받아 캐시, 받기 실패 · 모르는 kid는 거부(fail closed). `/mcp`는 MCP AUD, 나머지는 콘솔 AUD. 설정이 없으면 503 `access_not_connected`("연결 안 됨: Cloudflare Access"). `/v1/dev/tokens` · `BRAVO_DEV_AUTH` · 개발 시드 제거, `deploy/pilot/devauth` 삭제. 첫 로그인 역할은 설정 파일 허용 목록(`BRAVO_ACCESS_MEMBERS_FILE`, 마이그레이션 0006)만, 그 밖은 403 `unknown_principal`. 시험 `tests/test_auth.py`, `tests/test_landing.py`. 콘솔 자동 로그인(`console/src/state.tsx:66`)은 콘솔 갈래 몫, 열림 |
| A4 | 치명 | `platform/api/src/bravo_api/service.py:303,336-341`, `console App.tsx:194`, `api/types.ts:45`, `StateProgress.tsx:42` | 수집기가 설정 안 됐는데 "의존성 공백 없음"으로 처리해 ANALYZE를 건너뜀. 콘솔은 ANALYZE · PROPOSE · REHEARSE를 "파일럿 범위 밖"이라고 거짓 표시 | 플랫폼 API · 콘솔 | API 쪽 고침(21:2x): 수집기가 없거나 못 읽으면 ANALYZE에 머물고 `state_reason`=`collector_unconfigured` · `collector_*`. 수집기 실제 형식(`inventory/v1`의 `findings` · `unclassified` `tier_code`)을 읽음. `plan.analysis`는 `GET /v1/plans/{id}`에 있음. 시험 `tests/test_changes.py::test_unconfigured_collector_keeps_analyze_with_the_reason`. 콘솔 고침(23:3x, 닫힘): "파일럿 범위 밖" 표시(`OUT_OF_SCOPE_STATES`) 삭제. 현재 단계에 `state_reason`이 있으면 "멈춤"으로 그리고 끝난 단계로 세지 않음, 이유를 문장과 `state_reason=…`으로 보임(`collector_unconfigured` · `inventory_root_empty` · `collector_*`). 이벤트에 들어간 기록이 없는 앞 단계는 "지남"이 아니라 "기록 없음". 분석 칸은 `plan.analysis`를 읽고 인벤토리가 `ok`가 아니면 "분석 안 됨"과 이유. 홈 계획 표도 "ANALYZE · 멈춤"과 이유. 렌더 확인: `StateProgress`를 서버 렌더로 그려 ANALYZE+`collector_unconfigured`에서 ANALYZE가 `halted`(멈춤)이고 `done`이 아님(콘솔 `src/components/StateProgress.tsx`, `src/App.tsx`, `src/api/normalize.ts`) |
| A5 | 치명 | `platform/gate/src/bravo_gate/probes/objstore.py:27-28,271-273` | OBJ-06은 시간 초과 · 차단도 PASS, OBJ-05는 잘못된 자격도 "거부됨"으로 PASS | 보안 · 게이트 | 고침(20:46). OBJ-05는 `AccessDenied`일 때만 PASS, 자격 오류는 UNKNOWN. OBJ-06은 연결 거부 또는 https로 canary를 읽는 같은 자격이 http에서 `AccessDenied`일 때만 PASS, 시간 초과 · 끊김 · DNS는 UNKNOWN. `tests/test_verdict_rules.py` |
| A6 | 치명 | `docs/spec/TECH-STACK-SPEC.md:50` | "cloudflared 2026.10.7 (N12 통과 판)" 주장. N12는 11/3 예정 | 문서 | 고침(T29). "후보 판, N12 전" |
| B7 | 높음 | `platform/rehearsal/README.md:12`, `agent/harbor/_shared/verify.py:96-129` | 검증기는 제안 JSON 정적 검사뿐인데 README는 재배포 · 비공개 시험이라고 씀 | 리허설 | 고침(23:45, 리허설). 20:46: 정적 검사임을 기록으로 고정(`verifier_kind: static_rules`). 23:45: 동작 시험 추가. 제안서에 `rgw_bucket_policy`가 있으면 플랫폼이 별도 검증 Job(`behavioral.py`, `verify_job_s3.py`)을 `verify-<plan>-<n>-job`에서 돌림: RGW 시험 버킷 `bravo-rehearsal-verify`에 그 정책을 적용하고(버킷 이름 · 앱 주체만 바꿈), 서명 없는 GET은 `AccessDenied`, 소유자 키 GET은 200이어야 함. 대조 단계(열린 정책이면 익명 200), Job 이미지 digest 결속, 송신 차단 기록이 함께 맞아야 함. Job이 실제로 돌았을 때만 `verifier_kind: behavioral+static_rules`, 필요한데 못 돌면 PASS 대신 INFRA_ERROR. 정적 규칙은 그대로. 키는 OpenBao `secret/envs/<env>/rehearsal-s3`에서 Trial마다 읽음(D-26). 실측: Trial `t-3ac2e611c3bb42df92c0eeea6adc36cb`(온프렘 RKE2, Claude Agent SDK) PASS, 익명 GET `403 AccessDenied` · 소유자 GET 200, 열린 정책 사전 점검은 `anonymous_get_denied` 실패로 잡힘. 증거 `docs/evidence/s1-s2-bucket/`. 시험 `tests/test_behavioral.py`. 이미지 `rehearsal@sha256:1e1337fa…`(pin 변경, 푸시는 팀장). 남음: 평문 HTTP 거부(OBJ-06)는 이 Job이 보지 않음 |
| B8 | 높음 | `rehearsal/service.py:259`, `harbor_task.py:162-163` | `expected` 없이 materialize → 모든 역할 리허설이 INFRA_ERROR | 리허설 | 고침(리허설): `service.expected_for(trial)`이 봉투에서 role · plan_id · trial_id · attempt · evidence_ids를 만들어 `materialize`에 넘김. 시험 `test_agent_template_verifier_grades_frozen_artifact_and_binds_digest` |
| B9 | 높음 | `rehearsal/mcp_tools.py:48,144-155` | `repo_read` 루트가 고객 저장소가 아니라 템플릿 폴더 | 리허설 | 고침(리허설): 스냅숏이 없으면 `repo_unconfigured`, 템플릿 폴더는 쓰지 않음(`/src`도 비움). `PUT /v1/snapshots/{tenant}/{plan}`이 수집기 인벤토리 `input_digest`와 같은 파일 집합만 받고 `.env` 계열은 뺌. 올리기 도구 `python -m bravo_rehearsal.snapshot pack` · `upload`(수집기 `scan`과 대조). 시험 `test_snapshot_upload_is_bound_to_collector_inventory` |
| B10 | 높음 | `executor/cutover.py:614-656` | 첨부 객체 `NOT_CHECKED`인데 영수증 MATCH. 콘솔은 objects를 안 보임 | 데이터 · 컷오버, 콘솔 | 고침: 객체 `NOT_CHECKED`면 영수증 `MATCH_PARTIAL`, `verdict_parts`(tables · sequences · objects)를 함께 저장. 콘솔 고침(23:4x): 영수증 본문의 `tables[]` · `sequences[]` · `objects` · `verdict_parts`를 읽어 표별 비교와 부분 판정을 보이고, `MATCH_PARTIAL`은 초록 MATCH가 아니라 "일부만 확인"(첨부 객체 `NOT_CHECKED`와 이유 · 참조 수)으로 표시. 출발 · 목적은 영수증의 `source` · `target`(`src/components/CutoverPanel.tsx`, `src/api/normalize.ts`, `src/live/DemoAppScreen.tsx`) |
| B11 | 높음 | `api/service.py:127-157,262-276` | 환경의 `secret_ref`를 풀지 않음. 아무 ref나 등록해도 동작 | 플랫폼 API | 고침(플랫폼 API, 21:3x): 등록 때 플랫폼 OpenBao KV 메타데이터로 `secret_ref` 확인(값은 읽지 않음). 없으면 422 `secret_ref_not_found`, OpenBao에 못 닿으면 환경 `unverified` + 이유, 그 환경의 계획은 COLLECT에 머묾(`environment_secret_unverified:<이유>`). `envs/<환경 이름>/…` 참조 허용(0006). 시험 `tests/test_changes.py::test_secret_refs_resolve_in_openbao_or_block_collect`. 남음: API가 쓸 OpenBao 자격(AppRole, `secret/metadata/tenants/*` · `secret/metadata/envs/*` 읽기만)을 플랫폼 배포에 넣는 일 |
| B12 | 높음 | `deploy/pilot/scripts/contract_tests.sh:136-141` | E5가 구조상 항상 통과(Executor를 부르지 않음) | 데이터 · 컷오버 | 고침: `deploy/cloud/contract.sh` E5가 낮은 epoch로 `POST /v1/cutover`를 보내 `FENCE_REJECTED`(CONFIRMED_NOT_APPLIED) · ABORT_PRE_WRITE를 확인. Compose 판은 실행하지 않고 끝나게 함. 클라우드 실행은 아직 없음 |
| B13 | 높음 | `contract_tests.sh:147-153` | RESUME 실패를 무시하고 `effects`에 CONFIRMED_APPLIED와 지어낸 문장을 씀 | 데이터 · 컷오버 | 고침: 런북 단계는 RESUME 뒤 실제 `SHOW DATABASES` 행을 저장하고 `paused = 0`일 때만 진행. 스크립트가 `effects`를 쓰지 않음(해소 기록은 플랫폼을 거침) |
| B14 | 높음 | `tools/loadgen/src/bravo_loadgen/stats.py:73-75,92-93` | 응답 0건이어도 ok. 중복 검사가 구조상 0 | 데이터 · 컷오버 | 고침: `ok`는 확인 > 0, 보냄 = 확인 + 실패, 유실 · 중복 · 실패 0. 중복은 본문 표지로 셈. 시험 추가 |
| B15 | 높음 | `tools/loadgen/README.md:3` | 50 쓰기 · 150 읽기 req/s 주장. 실제 기본 20, 읽기 경로 없음 | 데이터 · 컷오버 | 고침: README를 있는 것(쓰기만, 기본 20/s)으로 바꿈 |
| B16 | 높음 | `gate/service.py:67-72`, `controls.py:119` | 설정 안 된 섹션은 질문을 안 만들어 일부만 보고 PASS | 보안 · 게이트 | 고침(20:46). 다섯 절을 늘 모두 시험. 설정 안 된 절은 `not_configured` UNKNOWN 행으로 남음. `test_http_api_contract` |
| B17 | 높음 | `gate/probes/objstore.py:159-175`, `postgres.py:107-150`, `platform.py:68-80,212-257` | OBJ-02/03 canary 없음, 앱 역할 없으면 DB-AUTH PASS, DNS 오류를 차단으로 셈, AUD 선택자 무시, SAR 오류 무시 | 보안 · 게이트 | 고침(20:46). OBJ-02/03도 canary 확인, 앱 역할이 없으면 DB-AUTH UNKNOWN(`app_role_missing`), DNS 오류는 `denied_path_inconclusive`, CloudTrail은 관리 이벤트 쓰기 기록 선택자만 인정, SAR `evaluationError`는 UNKNOWN. 앱 네임스페이스 · Secret이 없으면 K8s 질문 UNKNOWN(추가 발견) |
| B18 | 높음 | `collector/inventory/detector.py:83-88` | 증거 마스킹이 `aws_secret_access_key=`, `DB_PASSWORD=`, JSON password를 못 가림(D-26) | 보안 · 게이트 | 고침(20:46). 키 이름 안 어디에든 비밀 단어가 있으면 가림(`DB_PASSWORD=`, `aws_secret_access_key =`, JSON `"password"`, URL userinfo, `password=`). `test_masks_cover_secret_shapes` |
| B19 | 높음 | `collector/sql/collector_ro.sql:6-10`, `apps/demo/schema/00-bravo-init.sh:30-35`, `repl-setup.sh:35,42,55` | 비밀번호가 psql 인자 · SQL 문자열에 평문(D-26) | 보안 · 게이트, 데모 앱 | 수집기 부분 고침(20:46): `collector_ro.sql`은 `\getenv` + 클라이언트 쪽 SCRAM-SHA-256 검증값만 받고 평문이면 거부(`python -m bravo_collector scram-verifier`). 데모 앱 부분 고침(21:00): `00-bravo-init.sh`가 비밀번호마다 클라이언트 쪽 SCRAM-SHA-256 검증값을 만들고 `02_roles.sql`이 `\getenv`로 읽음(psql 인자 · SQL에 평문 없음, 시험 `test_roles_store_scram_verifiers_made_client_side`). `repl-setup.sh`는 인자 대신 psql 안에서 `\getenv`로 conninfo를 만들고, 그 세션은 `log_statement = none` · `log_min_error_statement = panic`. 남는 것: PG16은 비슈퍼유저 구독 소유자(`executor_ctl`)에게 conninfo 안 비밀번호를 요구해(`passfile=` 거부 실측) `CREATE SUBSCRIPTION` 한 문장과 `pg_subscription.subconninfo`(슈퍼유저만 읽음)에는 남음. 서버 · repl-setup 로그에 비밀번호 0건 실측 |
| B20 | 높음 | `deploy/pilot/.env`, `compose.data.yaml:136-144` | 로컬 파일의 장기 평문 비밀(Compose는 내렸음, 파일 정리 필요) | 플랫폼 EKS | 닫힘(23:3x): `deploy/pilot/.env`의 비밀 값 6개를 플랫폼 OpenBao 값과 해시로 대조해 하나도 쓰이지 않음을 확인(Compose 전용 값). 파일은 휴지통으로 옮김. `CLOUDFLARE_API_TOKEN`은 이미 비어 있었고 `secret/platform/cloudflare`에 있음. `keys.sh status` · `with-cloudflare` 정상 |
| B21 | 높음 | `deploy/CLOUD-E2E-CONTRACT.md:45`, `executor/README.md:31-32`, `14-provisioning-scenarios.md:89` | Cloudflare 가중치 전환을 동작하는 것처럼 씀. 실제는 미실행(`front.py` 주석) | 문서, 데이터 · 컷오버 | 표시는 문서 갈래가 넣음(코드만, 미실행). `front.py`는 `b-free`(플랫폼) 전환을 거부하고 `b-free-chat`만 다룸. 실제 실행(LB 포함)은 클라우드 앱 · 터널 연결 뒤. 문서 갈래(T29): `CLOUD-E2E-CONTRACT.md` 6절, `platform/executor/README.md`, `14-provisioning-scenarios.md`, `docs/demo/SCRIPT.md`에 표시함 / 닫힘: 실제 실행함. Cloudflare LB `b-free-chat` 가중치 10/50/100을 양방향으로 바꾸고 오류 0, 결함 주입 때 `REVERT_DST_0` · `ABORT_PRE_WRITE`로 되돌림(T17, `TARGET_SHARE` 판정). 증거는 9ca7af4의 클라우드 E2E 하네스 실행 기록 |
| B22 | 높음 | `.gitignore:1`(`runs/`), `smoke-…191253/s4-ac1.json`, `smoke-…194436` | 증거가 커밋되지 않고, 실패한 실행이 섞여 있음 | 문서 | 고침(T29). `docs/evidence/pilot-notebook/`에 스모크 11 · 계약 5 사본, `RUNS.tsv`에 실행별 PASS · FAIL(FAIL 3, 보류 4) |

중간 · 낮음(23–50)은 감사 원문 기준으로 각 담당에게 넘겼습니다.

보안 · 게이트 갈래가 고친 중간 · 낮음 항목(2026-11-02 20:46 KST):

| # | 상태 |
|---|---|
| 36 | 고침. `GET /v1/clusters`에서 읽기에 실패한 목록에 기대는 랜딩 존 행은 `present: null`, 클러스터 `status: partial`과 `failed_reads` |
| 37 | 고침. 코드 안 DSN 리터럴은 `코드 수정` 등급, 읽지 않은 파일은 `skipped[]`에 이유와 함께 |
| 38 | 고침. 수집기 오류 문자열(`model.failed`, `clusters.short_error`)과 게이트 증거(`result.error_text`)를 같은 패턴으로 가림 |
| 48 | 고침. 게이트가 쓴 시험 객체는 지우고 결과를 증거에 남김. HTTP GET(`/v1/controls`, `/v1/controls/report`)은 쓰기 시험을 하지 않음(`write_probe_not_allowed_here`). 쓰기 시험은 CLI `run --write-probes`만 | 요약: 문서 수치 불일치(23–25), 보안 보류를 빈 배열 상수로 반환(26), 호스트 이름 하드코딩(27), 심은 회귀를 스키마에 기본 포함(28), 시험용 충돌 훅이 제품 코드에(29–30, 29 고침: `BRAVO_ENABLE_FAULT_INJECTION=1` 없이 충돌 지점이 있으면 시작을 거부. 30 고침: COMMIT_POINT 관측이 플랫폼 DB의 6a 단계 · 구독 효과를 읽음), 쓰지 않는 RECORDED 모드(31), 워커로 넘기는 환경 변수 범위(32), 증거 행에 상수 값(33), 계정 0번 템플릿 · 로컬 digest(34), `/healthz`만 있고 준비 상태 없음(35), 읽기 실패를 "없음"으로(36), DSN 분류 오류 · 조용한 건너뛰기(37), 오류 문자열 미가림(38), 데모 앱이 DB 확인 전 "live" 표시(39), 링크 ack 없음(40), 차트 예시 값(41), `infra/README.md` 낡음(42), 출발지 ESO 역할 범위 과다(43), devauth 키 · 금고 보관 파일 한 곳 집중(44), 낮은 등급 6건(45–50).

중간 항목 가운데 리허설 몫의 상태(2026-11-02 20:46):

| # | 상태 |
|---|---|
| 32 | 고침: 워커 환경은 정확한 이름 목록(`sdk_options.ROUTE_VARS` 8개)과 운영자가 고른 `CODEX_HOME` · `HOME`뿐. 접두어 전달 없앰 |
| 33 | 고침: 증거 행의 `harbor_version`은 설치된 판(`importlib.metadata`), `quadrant`는 실제 실행에서 정함(검증기가 채점했으면 FIX, 아니면 ANALYZE), `backend`는 환경 유형, 메모에 `verifier_kind` |
| 34 | 리허설 몫 고침: `prompts/rehearsal-agent.md`(없는 도구 이름)와 `tasks/templates/fix-bucket-policy`(계정 `000000000000`, 옛 도구 설명)를 "준비 중"으로 표시, 코드가 읽지 않음을 명시. `platform/agent/harbor/*` 로컬 digest와 `allowlists/ci.yaml`의 0 digest는 에이전트 갈래 몫으로 넘김 |
| 35 | 고침: `GET /readyz`가 LLM · 클러스터 · 이미지 · 도구 계층 · OpenBao · 게이트를 항목별 이유로 보고(준비되면 200, 아니면 503) |

데모 앱 몫의 상태(2026-11-02 21:00, 이미지 `bravo-demo:0.3.0`):

| # | 상태 |
|---|---|
| 28 | 고침: 목적지 `GRANT SELECT ON messages TO PUBLIC`은 `BRAVO_PLANT_REGRESSION=1`일 때만(`03_dst.sql` `\if`), 초기화 로그에 "심은 시나리오 적용/없음"을 남김. 기본은 넣지 않음(시험 2개). 파일럿에서 게이트 1차 BLOCK 장면을 쓰려면 `compose.data.yaml`의 `dst-db`에 `BRAVO_PLANT_REGRESSION: "1"`이 필요(데이터 · 컷오버 몫) |
| 39 | 고침: `live` 배지는 서버가 쓰기 DB를 실제로 읽은 뒤(`db_ok`)에만, 그전은 "확인 중". `/readyz`는 PgBouncer로 DB를 읽은 적이 있고 마지막 성공이 30초 안일 때만 200. `APP_ENV_ID` 기본값 없앰(필수). 첨부는 `ObjectStore.ping()`(HeadBucket) 성공 뒤에만 알리고 15초마다 다시 확인, 실패 시 업로드 503 `attachments_unavailable`(시험 3개) |
| 41 | 고침: `chart/ci/*-values.yaml`은 파일 머리와 값마다 `EXAMPLE` 표시, 예시 값은 `*.example.invalid` · 0 digest · 계정 `000000000000`으로 바꿈. 배포 값은 `apps/demo/scripts/render-values.sh`가 `terraform output`과 실제 이미지 참조에서 만들고, 없으면 멈춤 |

문서 갈래(T29)가 고친 중간 항목(2026-11-02 21:0x KST). 41은 데모 앱 갈래가 고쳤습니다(위 표).

| # | 위치 | 고친 것 | 상태 |
|---|---|---|---|
| 23 | `deploy/pilot/CONTRACT.md`, `docs/architecture/12-db-migration-matrix.md`, `14-provisioning-scenarios.md` | 파일럿 DB를 "CNPG 이미지"에서 "PostgreSQL 공식 이미지, CNPG 운영자 없음"으로. ms 범위를 실행 파일 기준 76–139 ms로 | 고침 |
| 24 | `README.md` | "구현 전" 상태 문장을 실제 상태로, `.github/workflows/`를 실제 파일 이름으로, 클라우드 명령 문장을 고침 | 고침 |
| 25 | `docs/spec/SPEC-TRACE.md` | 작업 상태를 증거 기준으로("진행", 완료 0). T10 "목업으로" 기준에 목업 금지 지시를 덧붙임. T30 · T31 행 추가 | 고침 |
| 42 | `infra/README.md` | 시험 스크립트 경로(없음 표시), TopoLVM · SeaweedFS 제거, Terraform 1.17.1, D-26 비밀 경로, 랜딩 존 실제 설치 단계 | 고침 |

플랫폼 API 갈래가 고친 중간 항목(2026-11-02 21:4x KST):

| # | 상태 |
|---|---|
| 26 | 고침. `GET /v1/plans/{id}`의 `security_holds`는 `null`, `security_holds_reason`은 `not_implemented:D-20`. 빈 배열 상수를 없앰 |
| 44 | 플랫폼 API 몫 고침. `deploy/pilot/devauth`(키 · 토큰 · JWKS)를 지우고 제품 경로에서 개발 키를 읽는 코드를 없앰. 금고 보관 파일 집중은 열림 |
