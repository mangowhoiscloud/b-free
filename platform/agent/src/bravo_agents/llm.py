"""LLM 경로. 모델 키와 클라우드 자격은 LLM 게이트웨이에만 있습니다. 에이전트 Pod는 Trial 토큰만 가집니다.

두 경로를 환경 변수로 고릅니다(코드에 모델 ID · 지역 · 주소를 적지 않음).
- anthropic_gateway: 게이트웨이가 Anthropic API로 보냄(개발, 합성 데이터)
- bedrock_gateway: 게이트웨이가 Amazon Bedrock 도쿄 `jp.` 추론 프로필로 보냄(운영 경로). SDK는 Bedrock 모드,
  인증은 건너뛰고(CLAUDE_CODE_SKIP_BEDROCK_AUTH) Trial 토큰을 헤더로 붙임. AWS 자격은 게이트웨이만 가짐.
값이 하나라도 없으면 llm_unconfigured로 멈춥니다(fail-closed).
"""
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from bravo_agents.errors import AgentRunError

PROVIDER_ENV = "BRAVO_LLM_PROVIDER"
GATEWAY_ENV = "BRAVO_LLM_GATEWAY_URL"
MODEL_ENV = {"opus": "BRAVO_MODEL_OPUS", "haiku": "BRAVO_MODEL_HAIKU"}
REGION_ENV = "BRAVO_BEDROCK_REGION"
PROVIDERS = ("anthropic_gateway", "bedrock_gateway")


@dataclass(frozen=True)
class LlmRoute:
    provider: str
    gateway_url: str
    model: str
    region: str | None = None


def resolve_llm(tier: str, environ: Mapping[str, str] | None = None) -> LlmRoute:
    env = os.environ if environ is None else environ
    provider = env.get(PROVIDER_ENV, "")
    if provider not in PROVIDERS:
        raise AgentRunError("llm_unconfigured", f"{PROVIDER_ENV} must be one of {PROVIDERS}")
    url = env.get(GATEWAY_ENV, "")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise AgentRunError("llm_unconfigured", f"{GATEWAY_ENV} missing or not a URL")
    if tier not in MODEL_ENV:
        raise AgentRunError("llm_unconfigured", f"unknown model tier {tier}")
    model = env.get(MODEL_ENV[tier], "")
    if not model:
        raise AgentRunError("llm_unconfigured", f"{MODEL_ENV[tier]} not set")
    region = None
    if provider == "bedrock_gateway":
        region = env.get(REGION_ENV, "")
        if not region:
            raise AgentRunError("llm_unconfigured", f"{REGION_ENV} not set")
    return LlmRoute(provider, url, model, region)


# 상위 환경에서 새어 들어올 수 있는 자격을 빈 값으로 덮습니다. 에이전트 Pod에는 원래 없어야 합니다.
_SCRUB = ("ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
          "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG")


def agent_env(route: LlmRoute, trial_token: str, config_dir: str) -> dict[str, str]:
    """SDK가 띄우는 CLI 자식 프로세스의 환경 변수."""
    env = {k: "" for k in _SCRUB}
    env.update({
        "CLAUDE_CONFIG_DIR": config_dir,                 # 세션마다 새 디렉터리
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",          # transcript를 디스크에 남기지 않음
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # 게이트웨이 밖으로 나가는 부가 트래픽 끔
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    })
    if route.provider == "anthropic_gateway":
        env.update({"ANTHROPIC_BASE_URL": route.gateway_url, "ANTHROPIC_AUTH_TOKEN": trial_token})
    else:
        env.update({
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": route.gateway_url,
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "AWS_REGION": route.region or "",
            "ANTHROPIC_CUSTOM_HEADERS": f"Authorization: Bearer {trial_token}",
        })
    return env
