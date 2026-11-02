import pytest

from bravo_agents.errors import AgentRunError
from bravo_agents.llm import KEEP_ENV, SCRUB, agent_env, resolve_llm, subscription_env

GW = "https://llm-gw.bravo.svc"


def test_anthropic_gateway_route_and_env():
    r = resolve_llm("opus", {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": GW,
                             "BRAVO_MODEL_OPUS": "m-opus"})
    env = agent_env(r, "trial-tok", "/tmp/cfg")
    assert env["ANTHROPIC_BASE_URL"] == GW and env["ANTHROPIC_AUTH_TOKEN"] == "trial-tok"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "" and env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1" and env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg"
    assert env["ANTHROPIC_API_KEY"] == "" and env["AWS_SECRET_ACCESS_KEY"] == ""


def test_bedrock_gateway_route_and_env():
    r = resolve_llm("haiku", {"BRAVO_LLM_PROVIDER": "bedrock_gateway", "BRAVO_LLM_GATEWAY_URL": GW,
                              "BRAVO_MODEL_HAIKU": "m-haiku-jp-profile", "BRAVO_BEDROCK_REGION": "region-from-env"})
    env = agent_env(r, "trial-tok", "/tmp/cfg")
    assert r.model == "m-haiku-jp-profile"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == GW and env["AWS_REGION"] == "region-from-env"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "Authorization: Bearer trial-tok"
    assert env["AWS_ACCESS_KEY_ID"] == ""          # AWS 자격은 게이트웨이에만


@pytest.mark.parametrize("environ", [
    {},
    {"BRAVO_LLM_PROVIDER": "anthropic_gateway"},
    {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": GW},
    {"BRAVO_LLM_PROVIDER": "bedrock_gateway", "BRAVO_LLM_GATEWAY_URL": GW, "BRAVO_MODEL_OPUS": "m"},
    {"BRAVO_LLM_PROVIDER": "direct", "BRAVO_LLM_GATEWAY_URL": GW, "BRAVO_MODEL_OPUS": "m"},
    {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": "not a url", "BRAVO_MODEL_OPUS": "m"},
])
def test_missing_config_fails_closed(environ):
    with pytest.raises(AgentRunError) as e:
        resolve_llm("opus", environ)
    assert e.value.code == "llm_unconfigured" and e.value.infra


def test_default_provider_is_claude_subscription_and_model_comes_from_env():
    r = resolve_llm("haiku", {"BRAVO_MODEL_HAIKU": "claude-haiku-5-5"})
    assert (r.provider, r.model, r.gateway_url) == ("claude_subscription", "claude-haiku-5-5", "")
    r = resolve_llm("opus", {"BRAVO_LLM_PROVIDER": "openai_agents", "BRAVO_MODEL_OPUS": "m-openai"})
    assert (r.provider, r.model) == ("openai_agents", "m-openai")
    for env in ({}, {"BRAVO_LLM_PROVIDER": "openai_agents"}, {"BRAVO_LLM_PROVIDER": "codex_chatgpt",
                                                                "BRAVO_MODEL_OPUS": "m"}):
        with pytest.raises(AgentRunError) as e:
            resolve_llm("opus", env)
        assert e.value.code == "llm_unconfigured"


def test_subscription_env_scrubs_keys_and_inherited_session_vars():
    inherited = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "ANTHROPIC_API_KEY": "sk-ant-leak",
                 "ANTHROPIC_BASE_URL": "https://evil.example", "ANTHROPIC_AUTH_TOKEN": "x",
                 "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "host-session",
                 "CLAUDE_CODE_SIMPLE": "1", "HOME": "/Users/operator", "OPENAI_API_KEY": "sk-leak",
                 "BRAVO_LLM_BAO_SECRET_ID": "one-use", "SOME_OTHER": "v"}
    env = subscription_env("oauth-SECRET", "/tmp/cfg", inherited)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-SECRET"
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
              "CLAUDE_CODE_USE_VERTEX", "AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CODE_MESSAGING_TOKEN",
              "CLAUDE_CODE_SIMPLE", "OPENAI_API_KEY", "BRAVO_LLM_BAO_SECRET_ID", "SOME_OTHER"):
        assert env[k] == "", k
    assert "PATH" not in env and "LANG" not in env           # 상속 허용 이름은 덮지 않음
    assert env["CLAUDE_CONFIG_DIR"] == env["HOME"] == "/tmp/cfg"   # 운영자 ~/.claude를 읽지 않음
    assert env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1" and env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert {k for k, v in env.items() if v and k != "CLAUDE_CODE_OAUTH_TOKEN"}.isdisjoint(SCRUB)
    assert "PATH" in KEEP_ENV
    with pytest.raises(AgentRunError):
        subscription_env("", "/tmp/cfg", {})


def test_gateway_env_does_not_carry_subscription_token():
    r = resolve_llm("opus", {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": GW,
                             "BRAVO_MODEL_OPUS": "m"})
    env = agent_env(r, "trial-tok", "/tmp/cfg", {"CLAUDE_CODE_OAUTH_TOKEN": "leak"})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
