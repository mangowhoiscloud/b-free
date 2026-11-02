# 0009. LLM 경로: 모든 에이전트는 Agent SDK. 데모는 팀장 구독 토큰, 대체 엔진은 OpenAI Agents SDK, 운영은 플랫폼 키를 쥔 게이트웨이

- 상태: 데모 경로는 채택(데모 전용, 팀장 결정). 운영 목표는 **제안**
- 날짜: 2026-11-02(같은 날 개정)
- 정한 사람: 류지환(팀장). 원문(개정): "에이전트는 무조건 Agent SDK 사용해. 구독 정보는 내 계정 정보 쓰고. 코드로 구현해. 제발. 안되어있으면. OAI Agent SDK도 있잖아." 첫 결정 원문: "일단 OAI SDK + 내 구독 계정 사용해"
- 스펙 위치: §8.3("Claude Agent SDK … 플랫폼의 워커 프로세스로 실행 … 모델 키는 플랫폼에만"), §6 S2 시작 조건, T11, NFR-4, NFR-5, D-26, D-27
- 근거 문서: `docs/architecture/13-agents.md` 6절 "LLM 경로", `platform/agent/src/bravo_agents/llm.py` · `llm_secret.py` 모듈 설명, ADR 0007

## 맥락

스펙은 Claude Agent SDK 워커를 두고, 모델 키는 플랫폼에만 둡니다(§8.3). 운영 경로는 Amazon Bedrock 도쿄의 `jp.` 추론 프로필입니다. Anthropic API에는 일본 리전과 ISMAP 등록이 없기 때문입니다. 데모까지 게이트웨이와 모델 키를 다 갖추지 못했습니다.

첫 결정(같은 날)은 Codex SDK와 운영자 기기의 ChatGPT 로그인이었습니다. 팀장이 다시 정했습니다. Codex SDK는 Agent SDK가 아니므로 쓰지 않고, 모든 에이전트를 Agent SDK로 돌리며, 자격은 팀장 본인 구독을 씁니다. 킥오프 주제가 Beyond Lock-In이라 플랫폼 자신도 한 공급자에 묶이지 않도록 OpenAI Agents SDK를 같은 계약 뒤의 두 번째 엔진으로 둡니다.

## 선택지

| 안 | 장점 | 단점 |
|---|---|---|
| A. Claude Agent SDK + 팀장 구독 장기 OAuth 토큰(`claude setup-token`)을 플랫폼 OpenBao에 | 스펙의 SDK. 키 발급 없이 지금 동작. 자격이 플랫폼 금고 안(D-26) | 개인 구독이라 고객 서비스의 운영 경로가 될 수 없음. 사용 한도 |
| B. OpenAI Agents SDK + OpenAI API 키(OpenBao) | 두 번째 엔진. 같은 MCP 도구 계층 | API 키 필요. ChatGPT 구독 로그인 경로 없음 |
| C. Codex SDK + ChatGPT 로그인(첫 결정) | 키 발급 없음 | Agent SDK 아님. 자격이 운영자 기기에 있음(D-26 밖). 지움 |
| D. 게이트웨이(Bedrock 도쿄 `jp.` 프로필, 플랫폼 키) | 운영 경로. 데이터 위치 일본 | 게이트웨이 필요 |

## 결정

- **데모(채택, 데모 전용).** A를 씁니다. `BRAVO_LLM_PROVIDER=claude_subscription`(기본값). 토큰은 플랫폼 OpenBao `secret/envs/platform/llm-claude`(`oauth_token`)에만 있습니다. 리허설 서비스는 값을 읽지 않고, Trial마다 AppRole `llm-claude`(그 경로 읽기만)의 secret_id를 하나(60초, 한 번) 발급합니다. 워커가 그것으로 한 번 읽어 CLI 자식 프로세스 환경에만 넣습니다. 실행 전 init 검사로 도구 · MCP 서버 · 플러그인 · 자격 출처를 확인하고, 어긋나면 멈춥니다. 실측 canary로 cwd 밖 파일의 표식이 어디에도 나오지 않음을 확인했습니다(2026-11-02).
- **대체 엔진(채택).** B를 `openai_agents`로 둡니다. 키가 없으면 `llm_unconfigured`로 멈춥니다. ChatGPT 구독은 흉내 내지 않습니다.
- **지움.** C(`codex_chatgpt`)와 `[codex]` 의존성.
- **운영 목표(제안).** D. 플랫폼이 키를 쥔 LLM 게이트웨이에 비용 상한, FQDN 허용 목록, 반출 정책을 둡니다. 엔진을 바꿔도 에이전트 · 도구 계층 · 제안서 계약은 바뀌지 않습니다.

어느 경로에서도 판정과 실행은 코드가 합니다. 게이트, Executor, 상태 머신 전이는 LLM 클라이언트를 import하지 않습니다(NFR-5).

## 결과

- 쉬워지는 것: 데모 S2 장면을 키 발급 없이 실제 모델로 돌립니다. 자격은 D-26대로 플랫폼 금고에 있습니다.
- 어려워지는 것: 개인 구독은 고객에게 서비스하는 경로가 아닙니다. 발표에서 "데모 전용, 팀장 개인 구독, 운영 경로 아님"을 밝힙니다. 사용 한도에 걸리면 S2가 멈출 수 있습니다. 구독은 정액이라 USD 한도 대신 턴 · 시간 한도와 토큰 사용량 기록을 씁니다.
- 데모에는 합성 데이터만 보냅니다(`TECH-STACK-SPEC.md` 2.8절).
- 2026-11-02 상태: 실제 도구 계층에서 canary와 `discovery` 한 번이 통과했습니다(`platform/rehearsal/tests/test_live_agents.py`). 목적지 K3s에서 돈 Trial은 아직 없습니다.
- 되돌리려면: `BRAVO_LLM_PROVIDER`를 `openai_agents`, `anthropic_gateway`, `bedrock_gateway` 가운데 하나로 바꿉니다.
