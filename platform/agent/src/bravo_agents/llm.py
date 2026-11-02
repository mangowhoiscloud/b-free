"""LLM 경로. 모든 에이전트는 Agent SDK 위에서 돕니다(팀장 지시 2026-11-02 "에이전트는 무조건 Agent SDK 사용해").

공급자는 BRAVO_LLM_PROVIDER 하나로 고릅니다(없으면 claude_subscription). 코드에 모델 ID · 지역 · 주소를 적지 않습니다.
- claude_subscription(기본, 데모 경로): Claude Agent SDK. 자격은 팀장 구독의 장기 OAuth 토큰(`claude setup-token`)이고,
  플랫폼 OpenBao `secret/envs/platform/llm-claude`(필드 oauth_token)에 있습니다. 실행 때 한 번 쓰는 AppRole로 읽어
  SDK가 띄우는 CLI 자식 프로세스 환경(CLAUDE_CODE_OAUTH_TOKEN)에만 넣습니다(llm_secret.py).
- openai_agents(대체 엔진): OpenAI Agents SDK. API 키는 OpenBao `secret/envs/platform/llm-openai`(필드 api_key).
  OpenAI Agents SDK에는 ChatGPT 구독으로 로그인하는 지원 경로가 없습니다. API 키만 씁니다.
- anthropic_gateway, bedrock_gateway(운영 목표): Claude Agent SDK가 플랫폼 키를 가진 LLM 게이트웨이로 보냄.
  워커는 Trial 토큰만 가집니다. 운영 경로는 Bedrock 도쿄 `jp.` 추론 프로필입니다.
개인 구독은 고객에게 서비스하는 경로가 아닙니다(13-agents.md 6절). 값이 하나라도 없으면 llm_unconfigured로 멈춥니다.
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

SUBSCRIPTION = "claude_subscription"
OPENAI = "openai_agents"
GATEWAY_PROVIDERS = ("anthropic_gateway", "bedrock_gateway")
PROVIDERS = (SUBSCRIPTION, OPENAI, *GATEWAY_PROVIDERS)
DEFAULT_PROVIDER = SUBSCRIPTION
CLAUDE_PROVIDERS = (SUBSCRIPTION, *GATEWAY_PROVIDERS)   # Claude Agent SDK 경로
VAULT_PROVIDERS = (SUBSCRIPTION, OPENAI)                # 모델 자격을 실행 때 플랫폼 OpenBao에서 읽는 경로


@dataclass(frozen=True)
class LlmRoute:
    provider: str
    gateway_url: str
    model: str
    region: str | None = None


def provider_name(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(PROVIDER_ENV) or "").strip() or DEFAULT_PROVIDER


def resolve_llm(tier: str, environ: Mapping[str, str] | None = None) -> LlmRoute:
    env = os.environ if environ is None else environ
    provider = provider_name(env)
    if provider not in PROVIDERS:
        raise AgentRunError("llm_unconfigured", f"{PROVIDER_ENV} must be one of {PROVIDERS}")
    if tier not in MODEL_ENV:
        raise AgentRunError("llm_unconfigured", f"unknown model tier {tier}")
    model = (env.get(MODEL_ENV[tier]) or "").strip()
    if not model:
        raise AgentRunError("llm_unconfigured", f"{MODEL_ENV[tier]} not set")
    if provider in VAULT_PROVIDERS:
        return LlmRoute(provider, "", model)
    url = env.get(GATEWAY_ENV, "")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise AgentRunError("llm_unconfigured", f"{GATEWAY_ENV} missing or not a URL")
    region = None
    if provider == "bedrock_gateway":
        region = env.get(REGION_ENV, "")
        if not region:
            raise AgentRunError("llm_unconfigured", f"{REGION_ENV} not set")
    return LlmRoute(provider, url, model, region)


# CLI 자식 프로세스 환경. SDK는 워커의 os.environ을 복사한 뒤 options.env로 덮으므로, 이름을 지울 수는 없고
# 빈 값으로 덮습니다(CLI는 빈 값을 없는 것으로 봄). 아래 이름만 상속을 허용하고 나머지 상속 변수는 모두 비웁니다.
KEEP_ENV = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "USER", "LOGNAME", "SHELL", "TERM",
                      "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                      "https_proxy", "http_proxy", "no_proxy"})
# 상속 여부와 상관없이 늘 비우는 자격 · 경로 변수(Anthropic API 키 · 주소, Bedrock · Vertex · Foundry, 클라우드 자격).
SCRUB = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
         "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
         "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
         "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
         "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
         "CLOUD_ML_REGION", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
         "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG",
         "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "VAULT_TOKEN", "BAO_TOKEN",
         "BRAVO_LLM_BAO_ROLE_ID", "BRAVO_LLM_BAO_SECRET_ID", "BRAVO_REHEARSAL_TOKEN_KEY", "BRAVO_TRIAL_TOKEN")
_FLAGS = {
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",          # transcript를 디스크에 남기지 않음
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # 모델 호출 밖 부가 트래픽 끔
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


def _base_env(config_dir: str, inherited: Mapping[str, str] | None) -> dict[str, str]:
    parent = os.environ if inherited is None else inherited
    env = {k: "" for k in parent if k not in KEEP_ENV}
    env.update({k: "" for k in SCRUB})
    env.update(_FLAGS)
    # 세션마다 새 디렉터리. 운영자 ~/.claude(플러그인, MCP 서버, hook, 메모리, 로그인)를 읽지 않음.
    env.update({"CLAUDE_CONFIG_DIR": config_dir, "HOME": config_dir})
    return env


def agent_env(route: LlmRoute, trial_token: str, config_dir: str,
              inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """게이트웨이 경로의 CLI 환경. 게이트웨이는 Trial 토큰으로 워커를 인증합니다."""
    env = _base_env(config_dir, inherited)
    if route.provider == "anthropic_gateway":
        env.update({"ANTHROPIC_BASE_URL": route.gateway_url, "ANTHROPIC_AUTH_TOKEN": trial_token})
    elif route.provider == "bedrock_gateway":
        env.update({
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": route.gateway_url,
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "AWS_REGION": route.region or "",
            "ANTHROPIC_CUSTOM_HEADERS": f"Authorization: Bearer {trial_token}",
        })
    else:
        raise AgentRunError("llm_unconfigured", f"{route.provider} is not a gateway provider")
    return env


def subscription_env(oauth_token: str, config_dir: str, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """claude_subscription의 CLI 환경. 자격은 CLAUDE_CODE_OAUTH_TOKEN 하나뿐이고, API 키 · 주소 변수는 비웁니다."""
    if not oauth_token:
        raise AgentRunError("llm_unconfigured", "empty subscription token")
    env = _base_env(config_dir, inherited)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env
