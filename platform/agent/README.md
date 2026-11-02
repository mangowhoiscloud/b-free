# platform/agent

이전 한 건의 중간 작업을 맡는 에이전트 여섯(Discovery, DB, Governance, App Migration, CI, Report)입니다. 에이전트는 제안서만 내고, 판정과 실행은 상태 머신과 규칙이 합니다. 설계는 `docs/architecture/13-agents.md`, 편차는 D-27입니다. 작업은 T11, T16입니다.

## 폴더

| 경로 | 내용 |
|---|---|
| `src/bravo_agents/` | Agent SDK 위의 워커(Claude Agent SDK `claude_engine.py`, OpenAI Agents SDK `openai_engine.py`). 공개 인터페이스는 `run_agent(role, envelope, mcp_url, trial_token) -> Proposal` |
| `allowlists/<role>.yaml` | 역할별 허용 목록(기본 거부). 도구 계층이 집행하고, 워커는 SDK 도구 목록 · 한도 · 프롬프트의 책임 경계를 여기서 읽음 |
| `images/<name>/Dockerfile` | 샌드박스 이미지. 기반은 digest, 바이너리는 판 + 체크섬, 실행 사용자 65532, 패키지 관리자 없음. `expected-binaries.txt`가 감사 기준선 |
| `templates/bravo-app/` | CI 에이전트가 값만 채우는 고정 Helm 차트. `values.schema.json`이 값 범위를 막고, `imageRef`는 플랫폼만 넣음 |
| `harbor/<role>/` | Harbor 0.25.0 task(분리 검증기, 산출물은 `/out/proposal.json` 하나). `harbor/_shared/verify.py`가 원본 |
| `requirements.lock`, `harbor/requirements.lock` | 해시 고정 의존성(`uv pip compile pyproject.toml --extra openai --generate-hashes`). Harbor 휠도 sha256으로 고정 |

## 역할과 이미지

| 역할 | 상태 | 샌드박스 이미지 | 모델 등급 |
|---|---|---|---|
| `discovery` | ANALYZE | `discovery-agent`(rg, jq) | haiku |
| `db` | ANALYZE, WATCH | `governance-agent`(파일 작업만). SQL 리허설은 서버 쪽 `db_rehearse` | opus |
| `governance` | ANALYZE, PROPOSE, REHEARSE | `governance-agent`(jq, kubeconform) | opus |
| `app_migration` | PROPOSE, REHEARSE | `app-migration-agent`(python3, rg, diff) | opus |
| `ci` | PROPOSE, REHEARSE | `ci-agent`(python3 + pytest, helm, hadolint, kubeconform, trivy, syft) | opus |
| `report` | AWAIT_APPROVAL, DONE, DONE_ROLLED_BACK | `discovery-agent` | haiku |

`images/db-agent`는 에이전트 샌드박스가 아니라 도구 계층의 DB 러너(psql, pg_dump)입니다. 자격은 도구 계층이 OpenBao에서 받아 서버 쪽에서만 씁니다(D-26).

## LLM 경로

모든 에이전트는 Agent SDK 위에서 돕니다(팀장 지시 2026-11-02). 엔진은 `BRAVO_LLM_PROVIDER` 하나로 고르고, 계약(봉투, 도구 계층, 출력 모델, 검증, `plan_propose`)은 같습니다. 자세한 격리와 실측은 `docs/architecture/13-agents.md` 6절입니다.

| `BRAVO_LLM_PROVIDER` | 엔진 | 자격 | 쓰임 |
|---|---|---|---|
| `claude_subscription`(기본) | Claude Agent SDK 0.2.176 | 팀장 구독 OAuth 토큰. 플랫폼 OpenBao `secret/envs/platform/llm-claude` `oauth_token`. 워커가 한 번 쓰는 AppRole(`llm-claude`)로 읽어 CLI 환경에만 넣음 | 데모 경로 |
| `openai_agents` | OpenAI Agents SDK 0.20.0(`pip install '.[openai]'`) | OpenBao `secret/envs/platform/llm-openai`(AppRole `llm-openai`). `api_key`가 있으면 API 키, 없으면 팀장 ChatGPT 계정 세션(데모 전용, `keys.sh llm-login openai`) | 대체 엔진 |
| `anthropic_gateway`, `bedrock_gateway` | Claude Agent SDK | 게이트웨이가 플랫폼 키를 가짐. 워커는 Trial 토큰만 | 운영 목표(Bedrock 도쿄 `jp.` 프로필) |

- 개인 구독은 고객 서비스 경로가 아닙니다. 데모용이고, 운영 경로는 플랫폼 키를 가진 게이트웨이입니다.
- `openai_agents`의 ChatGPT 계정 모드는 OpenAI가 Agents SDK용으로 문서화하지 않은 Codex 백엔드를 Codex CLI와 같은 로그인으로 부릅니다. 데모 전용이고, 운영은 플랫폼이 쥔 API 키입니다. OpenAI가 막을 수 있고 약관 문제는 풀리지 않았습니다(13-agents.md 6절, ADR 0009).
- 구독은 정액이라 USD 한도를 두지 않습니다. 토큰 사용량만 남깁니다(`Proposal.usage`).

## 실행에 필요한 환경 변수

없으면 `llm_unconfigured` 또는 `evidence_unconfigured`로 멈춥니다. 값은 배포 설정에서 넣고 코드에 적지 않습니다.

| 변수 | 뜻 |
|---|---|
| `BRAVO_LLM_PROVIDER` | 위 표의 공급자. 비우면 `claude_subscription` |
| `BRAVO_MODEL_OPUS`, `BRAVO_MODEL_HAIKU` | 고른 엔진의 모델 ID(예: `claude-opus-5-5`, `claude-haiku-5-5`) |
| `BRAVO_BAO_ADDR`, `BRAVO_BAO_CACERT`, `BRAVO_BAO_KV_MOUNT` | `claude_subscription` · `openai_agents`: 자격을 읽을 플랫폼 OpenBao(https만) |
| `BRAVO_LLM_BAO_ROLE_ID`, `BRAVO_LLM_BAO_SECRET_ID` | 같은 경우: 한 번 쓰는 secret_id(60초). 리허설 서비스가 Trial마다 넣거나 `keys.sh llm-run`이 넣음 |
| `BRAVO_LLM_GATEWAY_URL` | 게이트웨이 공급자의 LLM 게이트웨이 주소 |
| `BRAVO_BEDROCK_REGION` | `bedrock_gateway`일 때 지역 |
| `BRAVO_EVIDENCE_URL` | 리허설 서비스의 증거 수신 API |
| `BRAVO_AGENT_ALLOWLIST_DIR` | 허용 목록 폴더(기본: 이 폴더의 `allowlists`) |

## 명령

```sh
make -C platform/agent test     # 단위 시험(SDK · 모델 호출 없음)
# 실측(구독 토큰, 도구 계층은 리허설 서비스): platform/rehearsal/tests/test_live_agents.py 머리말의 명령
make -C platform/agent images   # 샌드박스 이미지 빌드(로컬 아키텍처)
make -C platform/agent audit    # 이미지 감사: 바이너리 기준선, uid 65532, 패키지 관리자 · setuid 없음
make -C platform/agent smoke    # 허용 목록의 검사 argv를 실제 이미지에서 --network none으로 실행(14건)
```

## 지킬 것

- 허용 목록을 넓히는 변경(도구, 바이너리, 경로, 상태)은 리뷰를 받습니다. 로더는 실행 구간 상태, `kubectl` 작업, `plan_propose` 부여, 비밀 모양의 검사 환경 변수를 거부합니다.
- 샌드박스 · task 파일 · 프롬프트 · 도구 결과에 비밀 값을 넣지 않습니다(D-26).
- `harbor/_shared/verify.py`를 고치면 각 task의 `tests/`에 복사합니다. 시험이 같은 내용인지 확인합니다.
- 이미지의 바이너리를 바꾸면 `images/audit.sh --write-baseline <name>`으로 기준선을 다시 쓰고, 허용 목록 시험을 돌립니다.
