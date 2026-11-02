import asyncio

import pytest

from bravo_agents import AgentRunError, run_agent
from conftest import make_envelope


class _Sink:
    async def emit(self, e):
        pass


def test_fails_closed_without_model_route(monkeypatch):
    for k in ("BRAVO_LLM_PROVIDER", "BRAVO_LLM_GATEWAY_URL", "BRAVO_MODEL_OPUS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("governance", make_envelope(), "https://tools.bravo.svc/mcp", "tok",
                              evidence=_Sink()))
    assert e.value.code == "llm_unconfigured"


def test_fails_closed_without_evidence_sink(llm_env, monkeypatch):
    monkeypatch.delenv("BRAVO_EVIDENCE_URL", raising=False)
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("governance", make_envelope(), "https://tools.bravo.svc/mcp", "tok"))
    assert e.value.code == "evidence_unconfigured"


def test_refuses_scope_outside_allowlist(llm_env):
    env = make_envelope(tools=("inventory_describe", "db_inspect"))
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("governance", env, "https://tools.bravo.svc/mcp", "tok", evidence=_Sink()))
    assert e.value.code == "scope_exceeds_allowlist"


def test_refuses_past_deadline(llm_env):
    env = make_envelope(deadline="2020-01-01T00:00:00+00:00")
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("governance", env, "https://tools.bravo.svc/mcp", "tok", evidence=_Sink()))
    assert e.value.code == "timeout"


def test_accepts_dict_envelope_and_rejects_invalid(llm_env):
    """platform/rehearsal 워커는 봉투를 dict로 넘깁니다."""
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("governance", {"plan_id": "p-1"}, "https://tools.bravo.svc/mcp", "tok",
                              evidence=_Sink()))
    assert e.value.code == "envelope_invalid"


@pytest.mark.parametrize("provider", ["claude_subscription", "openai_agents", ""])
def test_vault_providers_fail_closed_without_openbao_delivery(monkeypatch, provider):
    """모델 ID는 있지만 OpenBao 전달 값이 없으면 SDK를 띄우기 전에 llm_unconfigured(가짜 자격 없음)."""
    for k in ("BRAVO_BAO_ADDR", "BRAVO_LLM_BAO_ROLE_ID", "BRAVO_LLM_BAO_SECRET_ID", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BRAVO_LLM_PROVIDER", provider)
    monkeypatch.setenv("BRAVO_MODEL_OPUS", "m-opus")
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("governance", make_envelope(), "https://tools.bravo.svc/mcp", "tok", evidence=_Sink()))
    assert e.value.code == "llm_unconfigured" and "openbao delivery not configured" in e.value.detail
