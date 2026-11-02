import uuid

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from bravo_agents.allowlist import load_allowlist
from bravo_agents.envelope import render_system_prompt
from bravo_agents.hooks import make_hooks
from bravo_agents.llm import LlmRoute
from bravo_agents.options import BUILTIN_DENY, build_options, session_id_for
from bravo_agents.schemas import output_schema
from conftest import make_envelope


class _Sink:
    def __init__(self):
        self.events = []

    async def emit(self, e):
        self.events.append(e)


def test_exact_sdk_options(envelope):
    al = load_allowlist("governance")
    route = LlmRoute("anthropic_gateway", "https://llm-gw.bravo.svc", "model-opus-from-env")
    hooks = make_hooks(envelope, _Sink())
    o = build_options(al, envelope, "https://tools.bravo.svc/mcp", "tok", route, "/tmp/cfg", hooks)
    assert isinstance(o, ClaudeAgentOptions)
    assert o.tools == []
    assert o.allowed_tools == ["mcp__platform__inventory_describe", "mcp__platform__policy_evaluate",
                               "mcp__platform__rehearsal_exec"]
    assert o.disallowed_tools == list(BUILTIN_DENY) + [
        "mcp__platform__repo_read", "mcp__platform__db_inspect", "mcp__platform__db_rehearse",
        "mcp__platform__controls_describe", "mcp__platform__evidence_read",
        "mcp__platform__plan_propose"]
    assert o.permission_mode == "dontAsk"
    assert o.setting_sources == []
    assert o.strict_mcp_config is True
    assert o.mcp_servers == {"platform": {"type": "http", "url": "https://tools.bravo.svc/mcp",
                                          "headers": {"Authorization": "Bearer tok"}}}
    assert o.max_turns == 30
    assert o.max_budget_usd == 1.5                       # min(허용 목록 2.0, 봉투 1.5)
    assert o.model == "model-opus-from-env"
    assert o.output_format == {"type": "json_schema", "schema": output_schema("governance")}
    assert o.session_id == session_id_for(envelope) and uuid.UUID(o.session_id).version == 5
    assert o.cwd == "/tmp/cfg" and o.env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg"
    assert o.system_prompt == render_system_prompt(envelope, al)
    assert set(o.hooks) == {"PreToolUse", "PostToolUse", "UserPromptSubmit"}
    assert all(isinstance(m, HookMatcher) for ms in o.hooks.values() for m in ms)
    assert o.resume is None and o.continue_conversation is False   # 세션을 이어 쓰지 않음


def test_session_id_bound_to_trial_attempt(envelope):
    assert session_id_for(envelope) == session_id_for(make_envelope())
    assert session_id_for(envelope) != session_id_for(make_envelope(attempt=3))
    assert session_id_for(envelope) != session_id_for(make_envelope(trial_id="t-0013-1"))


async def _run_hooks(envelope):
    sink = _Sink()
    h = make_hooks(envelope, sink)
    pre = await h["PreToolUse"]({"tool_name": "mcp__platform__rehearsal_exec", "tool_input": {"op": "x"}}, "tu1", None)
    post = await h["PostToolUse"]({"tool_name": "mcp__platform__rehearsal_exec"}, "tu1", None)
    ups = await h["UserPromptSubmit"]({"prompt": "go"}, None, None)
    return sink, pre, post, ups


def test_hooks_record_only_and_add_state(envelope):
    import asyncio
    sink, pre, post, ups = asyncio.run(_run_hooks(envelope))
    assert pre == {}                                      # 결정하지 않음(기록만)
    assert [e["seq"] for e in sink.events] == [1, 2]
    assert sink.events[0]["tool_input"] == {"op": "x"} and sink.events[0]["trial_id"] == "t-0012-2"
    assert post["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "attempt 2/3" in post["hookSpecificOutput"]["additionalContext"]
    assert ups["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_trial_token_stays_out_of_model_context(envelope):
    """D-26: 토큰은 MCP 헤더와 CLI 환경에만. 프롬프트 · 봉투 · 도구 정의에는 없음."""
    from bravo_agents.envelope import canonical_json, render_task_prompt
    al = load_allowlist("governance")
    route = LlmRoute("bedrock_gateway", "https://llm-gw.bravo.svc", "m", "r")
    o = build_options(al, envelope, "https://tools.bravo.svc/mcp", "tok-SECRET-123", route, "/tmp/cfg",
                      make_hooks(envelope, _Sink()))
    for text in (o.system_prompt, render_task_prompt(envelope), canonical_json(envelope).decode(),
                 str(o.output_format), str(o.allowed_tools), str(o.disallowed_tools)):
        assert "tok-SECRET-123" not in text
