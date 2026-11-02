"""openai_agents 엔진의 조립(모델 호출 없음): MCP 서버, 도구 필터, 클라이언트, 출력 스키마, 도구 검사."""
import asyncio

import pytest

pytest.importorskip("agents")

from bravo_agents import openai_engine as oe  # noqa: E402
from bravo_agents.errors import AgentRunError  # noqa: E402
from bravo_agents.schemas import OUTPUT_MODELS  # noqa: E402

GRANTED = ("inventory_describe", "repo_read", "rehearsal_exec")


def test_server_is_platform_streamable_http_with_trial_token_and_filter():
    s = oe.make_server("https://tools.bravo.svc/mcp", "trial-tok", GRANTED)
    assert type(s).__name__ == "MCPServerStreamableHttp" and s.name == "platform"
    assert s.params["url"] == "https://tools.bravo.svc/mcp"
    assert s.params["headers"] == {"Authorization": "Bearer trial-tok"}
    assert s.tool_filter == {"allowed_tool_names": list(GRANTED)}
    assert s.cache_tools_list is True


def test_run_config_pins_endpoint_key_and_disables_tracing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    rc = oe.make_run_config("sk-proj-from-openbao")
    client = rc.model_provider._client if hasattr(rc.model_provider, "_client") else None
    client = client or rc.model_provider._stored_client
    assert client.api_key == "sk-proj-from-openbao"
    assert str(client.base_url).rstrip("/") == oe.OPENAI_BASE_URL
    assert rc.tracing_disabled is True and rc.trace_include_sensitive_data is False


def test_output_schema_is_the_role_model():
    for role, model in OUTPUT_MODELS.items():
        s = oe.output_schema_for(model)
        assert s.output_type is model
        assert s.is_strict_json_schema() is (role not in ("governance", "ci"))  # 자유 형식 객체가 있는 역할만 비엄격


class _Tool:
    def __init__(self, name):
        self.name = name


class _Agent:
    def __init__(self, names, handoffs=()):
        self._names, self.handoffs = names, list(handoffs)

    async def get_all_tools(self, ctx):
        return [_Tool(n) for n in self._names]


def test_check_tools_requires_exact_grant():
    assert asyncio.run(oe.check_tools(_Agent(GRANTED), GRANTED)) == sorted(GRANTED)
    for agent, code in ((_Agent(GRANTED + ("shell",)), "unexpected_tools"),
                        (_Agent(GRANTED, handoffs=("other",)), "unexpected_tools"),
                        (_Agent(GRANTED[:1]), "mcp_unavailable")):
        with pytest.raises(AgentRunError) as e:
            asyncio.run(oe.check_tools(agent, GRANTED))
        assert e.value.code == code


def test_chatgpt_run_config_targets_codex_backend_with_account_headers(monkeypatch):
    """ChatGPT 계정 모드: Codex 백엔드, 접근 토큰, 계정 헤더, store=false, 암호화 reasoning, reasoning ID 빼기."""
    from test_chatgpt_auth import record

    from bravo_agents.chatgpt_auth import session_from_record

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    session = session_from_record(record())
    rc = oe.make_chatgpt_run_config("gpt-5.5", session, "sess-1")
    model = rc.model
    assert type(model).__name__ == "OpenAIResponsesModel" and model.model == "gpt-5.5"
    client = model._client
    assert client.api_key == session.access_token
    assert str(client.base_url).rstrip("/") == "https://chatgpt.com/backend-api/codex"
    headers = client.default_headers
    assert headers["ChatGPT-Account-ID"] == "acct-1234" and headers["session-id"] == "sess-1"
    assert rc.model_settings.store is False
    assert rc.model_settings.response_include == ["reasoning.encrypted_content"]
    assert rc.reasoning_item_id_policy == "omit"
    assert rc.tracing_disabled is True and rc.trace_include_sensitive_data is False


def test_error_detail_redacts_token_like_strings():
    class Boom(Exception):
        status_code = 400
        message = "bad instructions for " + "fake-token." * 6

    d = oe._error_detail(Boom())
    assert d.startswith("Boom 400 bad instructions") and "fake-token" not in d and "[redacted]" in d
