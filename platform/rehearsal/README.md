# bravo-rehearsal

에이전트 리허설 프로토타입입니다. 에이전트가 만든 이전 수정안을 Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름)에서 시험합니다. 판정은 에이전트가 고칠 수 없는 분리 검증기가 합니다.

SoftBank Hackathon 2026 본선 Team Bravo의 "에이전트 · 리허설" 영역입니다. 통합 설계 문서 v0.1의 T3, T11, T15, T16, T26에 해당합니다.

## 무엇을 하나

1. 상태 머신이 리허설을 요청하면, task 생성기가 task 템플릿과 고객 저장소 스냅숏(올라와 있을 때만)으로 Harbor task 폴더를 만듭니다.
2. Harbor Trial이 목적지 K3s의 리허설 네임스페이스를 환경으로 띄웁니다.
3. 에이전트 워커(`bravo_agents`)가 플랫폼에서 돌며 MCP 도구 계층으로만 환경을 다룹니다.
4. 분리 검증기가 검증 네임스페이스에서 고정된 제안서 JSON을 비공개 정적 규칙(`tests/verify.py`: 스키마, Trial 결속, 정책 규칙)으로 판정합니다. 제안서를 다시 배포하거나 실제 엔드포인트에 요청을 보내는 동작 시험(예: RGW 버킷 익명 GET 거부)은 아직 없습니다. 그래서 판정 기록에 `verifier_kind: static_rules`를 남깁니다.
5. 판정은 PASS, FAIL, INFRA_ERROR 가운데 하나이고, 증거와 함께 TSV에 한 줄로 남습니다. PASS는 "정적 규칙을 통과했다"는 뜻까지입니다.

에이전트는 어느 단계에서도 운영 자격 증명이나 실행 권한을 받지 않습니다. 승격은 사람이 승인합니다.

## 빠른 시작

```bash
make setup
make check
make oracle TASK=tasks/templates/fix-secret-injection
```

`make rehearse TASK=governance`는 서비스와 같은 Harbor Trial을 한 번 돌리고 `docs/evidence/rehearsals.tsv`에 한 줄을 남깁니다. 목적지 K3s 클러스터(저장소 상위의 `infra/envs/target-onprem/`), digest 고정 이미지, 모델 경로, 역할 허용 목록이 필요합니다. 하나라도 없으면 `verdict=BLOCKED`와 이유를 내고 아무것도 만들지 않습니다. 모델 공급자는 환경 변수(`BRAVO_LLM_PROVIDER`와 경로 변수)로만 고릅니다.

에이전트 실행은 언제나 실제 샌드박스가 있는 Harbor Trial 안에서만 합니다. 클러스터 없이 에이전트를 돌리는 방식은 두지 않습니다(팀장 결정 2026-11-02).

## 리허설 서비스(2026-11-02)

상태 머신(`platform/api`)이 시도마다 `POST /v1/trials`를 부릅니다. 서비스는 전제 조건을 먼저 확인하고(모델 키, 목적지 kubeconfig, digest 고정 이미지, Trial 토큰 키, task, 역할 허용 목록), 하나라도 없으면 아무것도 만들지 않고 이유를 돌려줍니다(첫 이유가 `llm_unconfigured`).

**런타임은 Harbor 0.25.0입니다.** 리허설 1회가 Harbor `Trial` 1회이고, 모든 에이전트는 Harbor 샌드박스(리허설 네임스페이스) 안에서만 일합니다.

| Harbor 요소 | 이 패키지 | 하는 일 |
|---|---|---|
| task 폴더 | `harbor_task.py` | 템플릿(`platform/agent/harbor/<task>`, 없으면 `tasks/templates/<task>`)을 Trial마다 복사. 정답 해 제외, 분리 검증기 · no-network 강제, `artifacts` 비움, 검증 진입점 추가 |
| `BaseEnvironment` | `harbor_env.py` | 에이전트 역할: `rehearsal-<plan>-<n>` + 기본선(PSA restricted, 기본 거부 + 같은 네임스페이스 + kube-dns, Quota, LimitRange, 토큰 없는 SA) + 샌드박스 Pod + 격리 시험. 검증 역할: `verify-<plan>-<n>` + 검증 Pod + 고정 산출물. 생성 · 삭제는 프로비저너, exec와 파일 전송(exec 위 tar 스트림)은 네임스페이스별 exec 계정 |
| `BaseAgent` | `harbor_agent.py` | 워커 프로세스(`bravo_agents.run_agent`)를 띄우고 기다림. 워커는 Trial 토큰, 상태 봉투(`StateEnvelope`), 모델 경로 설정만 받음(모델 키 · kubeconfig 없음). 전제가 없으면 이유로 실패하고 결과를 꾸며내지 않음. 궤적(ATIF)을 `agent/trajectory.json`에 씀 |
| 분리 검증기 · 판정 | `harbor_run.py` | Harbor의 `environment_mode="separate"` 검증 결과를 받음. 검증 Pod UID · 이미지 digest · 산출물 digest가 맞을 때만 PASS/FAIL, 아니면 INFRA_ERROR. 에이전트가 미리 써 둔 reward는 무시(Harbor issue #2551). 판정에 `verifier_kind`(지금은 `static_rules`)를 함께 기록 |

**`bravo_agents` 연결(2026-11-02).** `platform/agent`의 패키지를 경로 의존성으로 씁니다(`uv.lock`).

| 항목 | 어디서 | 규칙 |
|---|---|---|
| 허용 목록 | `allowlist.py` → `bravo_agents.load_allowlist(role)` | `platform/agent/allowlists/<role>.yaml`. 파일이 없으면 `role_allowlist_missing`, 형식 · 스키마 위반이면 `role_allowlist_invalid`로 모든 호출 거부. 어떤 역할도 kubectl을 받지 않음. `plan_propose`는 워커 전용이라 모델의 도구 목록에 나오지 않음 |
| 샌드박스 작업 | `tool_layer.authorize_sandbox` | read_file · list_dir · write_file · run_check만. 경로는 정규화 뒤 역할의 읽기 · 쓰기 루트 안, 쓰기 크기 상한, 검사는 `check_argv`(선언한 자리에만 인자, `-`로 시작하는 값 거부)와 검사별 env · cwd · timeout |
| 제안서 | `proposal.check` | `bravo_agents.schemas.OUTPUT_MODELS[role]`로 검증 → Trial 결속(plan_id, trial_id, attempt, 봉투의 증거 ID) → 린트(비밀 모양 값, 익명 · 와일드카드 허용, 클러스터 범위 RBAC, 예약 이름, 위험 SQL) |
| 상태 봉투 | `service.build_envelope` | `StateEnvelope` 형식. 승인 범위는 역할 허용 목록 그대로. plan_id는 `p-` + 소문자 UUID. 요청의 `state`가 역할의 상태 목록에 없으면 `state_not_allowed_for_role` |
| 증거 수신 | `POST /v1/evidence` | 워커 hook 사건(Trial 토큰 인증, seq 중복은 409). 권위 없는 기록이고 도구 입력은 해시만 남김 |

**서버 쪽 도구(D-26).** 자격은 호출 때 플랫폼 OpenBao(KV v2)에서 읽고 이 프로세스 밖으로 내보내지 않습니다. 결과의 모든 문자열은 비밀 모양 값을 가립니다. 설정이 없으면 이유(`gate_unconfigured`, `openbao_unconfigured`, `secret_missing` 등)로 거부합니다. OpenBao 변수 이름은 Executor와 같습니다(`BRAVO_BAO_ADDR`, `BRAVO_BAO_CACERT`, `BRAVO_BAO_ROLE_ID`, `BRAVO_BAO_SECRET_ID`). 파일럿은 AppRole `rehearsal`(정책 `deploy/pilot/bao/policies/platform-rehearsal.hcl`, 주기 토큰 1시간, secret_id 60초 · 1회)이고, 서비스는 시작 때 로그인한 뒤 secret_id를 버리고 토큰을 갱신합니다.

| 도구 | 하는 일 | 고객 쪽에 대해 |
|---|---|---|
| `controls_describe` | 게이트 `GET /v1/controls?source=&target=`(파일럿 `http://gate:8084`). source · target은 Trial의 `source_env` · `target_env`(파일럿 `pilot-src` · `pilot-dst`)에서, 도구 입력에서 받지 않음 | 읽기만 |
| `evidence_read` | 봉투에 있는 ID만. `gate:<uuid>` · `analysis:<plan uuid>` · `rehearsal:<uuid>`는 플랫폼 DB에서 `bravo_rehearsal_ro`(열 단위 권한, 테넌트 RLS, 이 계획의 행만, 읽기 전용 세션)로, 그 밖의 ID는 증거 저장소(`BRAVO_EVIDENCE_DIR`)의 JSON 파일 | 읽기만 |
| `db_inspect` | 허용 목록의 `db_queries` 가운데 고정 카탈로그 SQL 하나를 `collector_ro`(`secret/envs/<env>/collector-ro`)로, READ ONLY 트랜잭션 · statement_timeout | 읽기만 |
| `db_rehearse` | 합성 DB(`rehearsal-synthetic-db`, 역할 `rehearse_rw`는 스키마 `app`만 소유)에서 트랜잭션 하나로 실행하고 늘 ROLLBACK. Trial마다 새 DB를 만드는 방식보다 단순해서 골랐음(CREATE DATABASE 권한 · 정리 불필요). 트랜잭션 제어, SET, DO, COPY, CREATE DATABASE · ROLE, 서버 파일 · 프로세스 함수, E'' 문자열은 보내기 전에 거부. 결과는 문장별 상태 · 행 수 · SQLSTATE만 | 합성 DB만 |

DB 역할은 `sql/`에 있습니다. `rehearsal_evidence_ro.sql`(플랫폼 DB, API 마이그레이션 뒤 소유자로 적용, 암호는 환경 변수에서 psql `\getenv`)과 `synthetic-init.sh`(합성 DB 초기화).

**모델 경로.** 모든 에이전트는 Agent SDK 위에서 돕니다(팀장 지시 2026-11-02). 공급자는 `BRAVO_LLM_PROVIDER`로만 고르고(비우면 `claude_subscription`), `bravo_agents.llm.resolve_llm`이 검사합니다. 자세한 격리는 `docs/architecture/13-agents.md` 6절입니다.

| 공급자 | 변수 | 비고 |
|---|---|---|
| `claude_subscription`(기본, 데모) | `BRAVO_MODEL_OPUS=claude-opus-5-5`, `BRAVO_MODEL_HAIKU=claude-haiku-5-5`, 서비스의 `BRAVO_BAO_*` | Claude Agent SDK + 팀장 구독 토큰(OpenBao `secret/envs/platform/llm-claude` `oauth_token`). 개인 구독이라 고객 서비스 경로가 아님 |
| `openai_agents`(대체 엔진) | `BRAVO_MODEL_*`(OpenAI 모델 ID), 서비스의 `BRAVO_BAO_*` | OpenAI Agents SDK 0.20.0 + OpenBao `secret/envs/platform/llm-openai`. `api_key`가 있으면 API 키, 없으면 팀장 ChatGPT 계정 세션(데모 전용, 문서화되지 않은 Codex 백엔드, ADR 0009) |
| `bedrock_gateway`(운영 목표) | `BRAVO_LLM_GATEWAY_URL`, `BRAVO_MODEL_OPUS=jp.anthropic.claude-opus-5-5`, `BRAVO_MODEL_HAIKU=jp.anthropic.claude-haiku-5-5`, `BRAVO_BEDROCK_REGION=ap-northeast-1` | 플랫폼 키를 가진 게이트웨이. 설정만, 이 패키지에서 실호출하지 않음 |
| `anthropic_gateway` | `BRAVO_LLM_GATEWAY_URL`, `BRAVO_MODEL_*` | 개발용 |

**자격 전달.** 서비스는 모델 자격을 읽지 않습니다. KV 메타데이터로 있는지만 보고(`/readyz`의 `llm_token`, 없으면 `POST /v1/trials`가 `llm_token_missing`), Trial마다 공급자 AppRole(`llm-claude` · `llm-openai`)의 secret_id를 하나(60초, 한 번) 발급해 워커에 넘깁니다. 워커가 그것으로 값을 한 번 읽고 토큰을 폐기합니다(`bravo_agents.llm_secret`). 서비스 정책은 `deploy/pilot/bao/policies/platform-rehearsal.hcl`(메타데이터 읽기, secret_id 발급), 워커 정책은 `platform-llm-*.hcl`(그 경로 읽기. `llm-openai`는 갱신한 ChatGPT 세션 되쓰기용 `update`가 하나 더)입니다. 정책을 바꿨으면 `rehearsal-bao.sh setup`을 다시 돌립니다. AppRole은 `keys.sh llm-run`이 처음 쓸 때 만듭니다.

워커 환경에는 정확한 이름 목록만 들어갑니다: 경로 변수(`sdk_options.ROUTE_VARS`), OpenBao 공급자일 때 서비스의 OpenBao 주소 · CA · 마운트와 한 번 쓰는 `BRAVO_LLM_BAO_ROLE_ID` · `BRAVO_LLM_BAO_SECRET_ID`, Trial 값. 접두어나 운영자 지정으로 넘기는 변수는 없습니다. Trial 토큰 · 도구 계층 주소 · 봉투는 서비스 값이 늘 우선합니다.

**실측 시험.** `tests/test_live_agents.py`(표시 `llm`, `BRAVO_LIVE_LLM=1`일 때만). 이 서비스의 실제 도구 계층에 Trial 하나를 두고 canary(cwd 밖 파일의 표식이 어디에도 나오지 않는지)와 `discovery` 한 번(`run_agent` → `plan_propose`)을 돌립니다. secret_id가 한 번 쓰는 값이라 시험 하나에 `keys.sh llm-run` 한 번입니다. 명령은 파일 머리말에 있습니다.

**파일럿 컨테이너.** `Dockerfile`(빌드 맥락은 `platform/`, `uv.lock` 그대로, non-root, 읽기 전용 루트)과 `deploy/pilot/compose.rehearsal.yaml`(서비스, 합성 DB, 증거 역할 한 번 적용). 시작과 한 번만 하는 준비는 `deploy/pilot/README.md`의 "리허설 서비스" 절에 있습니다. 운영자 기기에서 리허설 한 번은 `python -m bravo_rehearsal.runner --task governance`로 돌립니다.

Trial 기록은 `BRAVO_REHEARSAL_TRIALS_DIR`(기본 `runs/`) 아래 `<trial_id>/`(Harbor 형식)와 `<trial_id>.task/`에 남습니다. 의존성은 `uv.lock`에 해시로 고정합니다(Python 3.12 이상, Harbor 0.25.0 요구).

```bash
uv sync --extra service --extra dev                   # uv.lock 기준(해시 고정)
.venv/bin/python -m pytest -q                        # 모델 · 클러스터가 필요한 시험은 이유와 함께 건너뜀
.venv/bin/uvicorn bravo_rehearsal.service:create_app --factory --port 8090
```

환경 변수는 `.env.example`에 있습니다. 워커에는 모델 경로 설정(정확한 이름 목록)과 Trial 토큰만 넘깁니다. 게이트웨이 토큰과 모델 키는 넘기지 않습니다. `GET /readyz`는 Trial을 지금 돌릴 수 있는지 항목별 이유로 답합니다(LLM, 클러스터, 이미지, 도구 계층, OpenBao, 게이트, 모델 자격 저장 여부 `llm_token`; 값은 없음).

**고객 저장소 스냅숏(`repo_read`, `/src`).** `repo_read`는 계획마다 올린 고객 저장소 스냅숏만 읽고, 없으면 `repo_unconfigured`입니다. task 템플릿의 예시 파일은 쓰지 않습니다. 스냅숏은 수집기 인벤토리(`GET /v1/inventory`의 `input_digest`)와 같은 파일 집합이어야 받습니다. `.env` 계열은 확인한 뒤 빼고, 읽을 때 비밀 모양 값을 가립니다. 올리기: `PYTHONPATH=platform/rehearsal/src uv run --project platform/collector python -m bravo_rehearsal.snapshot pack --repo <저장소> --out <파일>` → `python -m bravo_rehearsal.snapshot upload ...`(`PUT /v1/snapshots/{tenant}/{plan}`, 헤더 `X-Bravo-Inventory-Digest`). Trial 요청에 `inventory_digest`를 넣으면 그 스냅숏에만 묶입니다. `docker` 표시 시험은 실제 postgres:16 · OpenBao 컨테이너를 띄우고, Docker가 없으면 `docker_unavailable`로 건너뜁니다.

## 문서

- 에이전트 작업 규칙: `AGENTS.md`
- 문서 지도: `docs/README.md`
- 접근안과 근거 리서치(저장소 밖 팀 연구 폴더): `research/deliverables/2026-11-02-harbor-approach.md`
