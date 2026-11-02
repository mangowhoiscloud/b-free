import pytest

from bravo_agents.errors import AgentRunError
from bravo_agents.llm import agent_env, resolve_llm

GW = "https://llm-gw.bravo.svc"


def test_anthropic_gateway_route_and_env():
    r = resolve_llm("opus", {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": GW,
                             "BRAVO_MODEL_OPUS": "m-opus"})
    env = agent_env(r, "trial-tok", "/tmp/cfg")
    assert env["ANTHROPIC_BASE_URL"] == GW and env["ANTHROPIC_AUTH_TOKEN"] == "trial-tok"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
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
