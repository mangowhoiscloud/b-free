"""codex_chatgpt 경로의 오프라인 시험(모델 · Codex 호출 없음). 실측 격리 검사는 isolation_preflight가 실행마다 합니다."""
import asyncio

import pytest

from bravo_agents import codex_chatgpt as cx
from bravo_agents.errors import AgentRunError
from bravo_agents.runner import run_agent
from bravo_agents.schemas import output_schema
from conftest import make_envelope

GRANTED = ("inventory_describe", "repo_read", "rehearsal_exec")


def test_model_comes_from_env_only():
    with pytest.raises(AgentRunError) as e:
        cx.resolve_codex({})
    assert e.value.code == "llm_unconfigured"
    assert cx.resolve_codex({"BRAVO_CODEX_MODEL": "m-from-env"}).model == "m-from-env"
    with pytest.raises(AgentRunError):
        cx.resolve_codex({"BRAVO_CODEX_MODEL": "m", "BRAVO_CODEX_EFFORT": "max"})


def test_run_agent_fails_closed_without_model(monkeypatch):
    monkeypatch.setenv("BRAVO_LLM_PROVIDER", "codex_chatgpt")
    monkeypatch.delenv("BRAVO_CODEX_MODEL", raising=False)
    env = make_envelope(role="discovery", state="ANALYZE", tools=("inventory_describe",), task="classify-deps",
                        approved_scope={"mcp_tools": ["inventory_describe"], "sandbox_ops": [], "checks": []})
    with pytest.raises(AgentRunError) as e:
        asyncio.run(run_agent("discovery", env, "http://127.0.0.1:1/mcp", "t", evidence=object(), submit=False))
    assert e.value.code == "llm_unconfigured" and e.value.infra


def test_overrides_turn_off_builtins_and_carry_no_token():
    ov = cx.isolation_overrides("http://tool-layer/mcp", GRANTED)
    for key in ("features.shell_tool=false", "features.unified_exec=false", "features.view_image=false",
                "features.apps=false", "features.plugins=false", "features.multi_agent=false",
                "features.code_mode_host=false", 'web_search="disabled"', 'sandbox_mode="read-only"',
                'approval_policy="never"', "notify=[]", "project_doc_max_bytes=0"):
        assert key in ov
    mcp = next(o for o in ov if o.startswith("mcp_servers="))
    assert 'bearer_token_env_var="BRAVO_TRIAL_TOKEN"' in mcp and "secret-token" not in mcp
    assert '"plan_propose"' not in mcp
    assert cx.codex_env("secret-token")["BRAVO_TRIAL_TOKEN"] == "secret-token"
    assert cx.codex_env("x")["OPENAI_API_KEY"] == ""


def test_catalog_patch_removes_model_builtins():
    assert cx.CATALOG_PATCH["tool_mode"] == "direct"
    assert cx.CATALOG_PATCH["shell_type"] == "disabled"
    assert cx.CATALOG_PATCH["apply_patch_tool_type"] is None


def test_request_tool_list_check():
    body = {"input": [{"type": "additional_tools", "tools": [
        {"type": "namespace", "name": "functions", "tools": [{"type": "function", "name": "read_mcp_resource"}]},
        {"type": "namespace", "name": "mcp__platform", "tools": [{"type": "function", "name": "repo_read"}]},
        {"type": "function", "name": "exec"}]}]}
    names = cx._flatten_tool_names(cx.request_tools(body))
    assert names == ["functions.read_mcp_resource", "mcp__platform.repo_read", "exec"]
    extra = [n for n in names if not cx.is_platform_tool(n, GRANTED) and n not in cx.MCP_RESOURCE_TOOLS]
    assert extra == ["exec"]


def test_executed_items_outside_platform_are_flagged():
    ok = [{"type": "userMessage"}, {"type": "reasoning"}, {"type": "agentMessage"},
          {"type": "mcpToolCall", "server": "platform", "tool": "repo_read"}]
    assert cx.check_items(ok, GRANTED) == []
    bad = ok + [{"type": "commandExecution"}, {"type": "mcpToolCall", "server": "exa", "tool": "search"},
                {"type": "mcpToolCall", "server": "platform", "tool": "plan_propose"}]
    assert cx.check_items(bad, GRANTED) == ["commandExecution", "mcp:exa/search", "mcp:platform/plan_propose"]


def test_strict_schema_requires_every_property():
    s = cx.strict_schema(output_schema("discovery"))
    assert set(s["required"]) == set(s["properties"])
    assert "evidence_refs" in s["required"]
    assert output_schema("discovery")["required"] != s["required"]  # 원본은 그대로
