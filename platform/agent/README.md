# platform/agent

이전 한 건의 중간 작업을 맡는 에이전트 여섯(Discovery, DB, Governance, App Migration, CI, Report)입니다. 에이전트는 제안서만 내고, 판정과 실행은 상태 머신과 규칙이 합니다. 설계는 `docs/architecture/13-agents.md`, 편차는 D-27입니다. 작업은 T11, T16입니다.

## 폴더

| 경로 | 내용 |
|---|---|
| `src/bravo_agents/` | Claude Agent SDK 위의 워커. 공개 인터페이스는 `run_agent(role, envelope, mcp_url, trial_token) -> Proposal` |
| `allowlists/<role>.yaml` | 역할별 허용 목록(기본 거부). 도구 계층이 집행하고, 워커는 SDK 도구 목록 · 한도 · 프롬프트의 책임 경계를 여기서 읽음 |
| `images/<name>/Dockerfile` | 샌드박스 이미지. 기반은 digest, 바이너리는 판 + 체크섬, 실행 사용자 65532, 패키지 관리자 없음. `expected-binaries.txt`가 감사 기준선 |
| `templates/bravo-app/` | CI 에이전트가 값만 채우는 고정 Helm 차트. `values.schema.json`이 값 범위를 막고, `imageRef`는 플랫폼만 넣음 |
| `harbor/<role>/` | Harbor 0.25.0 task(분리 검증기, 산출물은 `/out/proposal.json` 하나). `harbor/_shared/verify.py`가 원본 |
| `requirements.lock`, `harbor/requirements.lock` | 해시 고정 의존성(`uv pip compile --generate-hashes`). Harbor 휠도 sha256으로 고정 |

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

## 실행에 필요한 환경 변수

없으면 `llm_unconfigured` 또는 `evidence_unconfigured`로 멈춥니다. 값은 배포 설정에서 넣고 코드에 적지 않습니다.

| 변수 | 뜻 |
|---|---|
| `BRAVO_LLM_PROVIDER` | `anthropic_gateway` 또는 `bedrock_gateway`(운영: Bedrock 도쿄 `jp.` 추론 프로필). `codex_chatgpt`는 파일럿 · 데모 전용(`pip install '.[codex]'`, 격리 사전 검사를 통과해야 실행) |
| `BRAVO_CODEX_MODEL`, `BRAVO_CODEX_EFFORT`, `BRAVO_CODEX_BIN` | `codex_chatgpt`일 때 모델(필수), 추론 강도 · Codex 실행 파일(선택) |
| `BRAVO_LLM_GATEWAY_URL` | LLM 게이트웨이 주소. 모델 키와 클라우드 자격은 게이트웨이에만 |
| `BRAVO_MODEL_OPUS`, `BRAVO_MODEL_HAIKU` | 모델 ID |
| `BRAVO_BEDROCK_REGION` | `bedrock_gateway`일 때 지역 |
| `BRAVO_EVIDENCE_URL` | 리허설 서비스의 증거 수신 API |
| `BRAVO_AGENT_ALLOWLIST_DIR` | 허용 목록 폴더(기본: 이 폴더의 `allowlists`) |

## 명령

```sh
make -C platform/agent test     # 단위 시험(SDK · 모델 호출 없음)
make -C platform/agent images   # 샌드박스 이미지 빌드(로컬 아키텍처)
make -C platform/agent audit    # 이미지 감사: 바이너리 기준선, uid 65532, 패키지 관리자 · setuid 없음
make -C platform/agent smoke    # 허용 목록의 검사 argv를 실제 이미지에서 --network none으로 실행(14건)
```

## 지킬 것

- 허용 목록을 넓히는 변경(도구, 바이너리, 경로, 상태)은 리뷰를 받습니다. 로더는 실행 구간 상태, `kubectl` 작업, `plan_propose` 부여, 비밀 모양의 검사 환경 변수를 거부합니다.
- 샌드박스 · task 파일 · 프롬프트 · 도구 결과에 비밀 값을 넣지 않습니다(D-26).
- `harbor/_shared/verify.py`를 고치면 각 task의 `tests/`에 복사합니다. 시험이 같은 내용인지 확인합니다.
- 이미지의 바이너리를 바꾸면 `images/audit.sh --write-baseline <name>`으로 기준선을 다시 쓰고, 허용 목록 시험을 돌립니다.
