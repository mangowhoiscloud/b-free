"""ClaudeAgentOptions 조립(Claude Agent SDK 경로: claude_subscription, 게이트웨이). 시험이 필드 값을 그대로 확인합니다."""
import uuid
from typing import Callable

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from bravo_agents.allowlist import PLATFORM_TOOLS, Allowlist
from bravo_agents.envelope import StateEnvelope, render_system_prompt
from bravo_agents.hooks import Hook
from bravo_agents.llm import SUBSCRIPTION, LlmRoute, agent_env
from bravo_agents.schemas import output_schema

MCP_SERVER = "platform"
# 내장 도구는 tools=[]로 빼고, 남을 수 있는 이름을 정의째 지웁니다(이름만 쓰면 도구 정의를 요청에서 제거).
BUILTIN_DENY = ("Agent", "Task", "TaskStop", "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit",
                "MultiEdit", "NotebookEdit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "ToolSearch",
                "ListMcpResourcesTool", "ReadMcpResourceTool", "Skill", "SlashCommand", "ExitPlanMode")
SESSION_NS = uuid.UUID("6f1d8a6e-3b0e-4f43-9a55-1c2b7d0b9e01")  # Trial ↔ 세션 ID 결속용 고정 네임스페이스


def mcp_tool(name: str) -> str:
    return f"mcp__{MCP_SERVER}__{name}"


def session_id_for(envelope: StateEnvelope) -> str:
    """같은 (trial, attempt)는 같은 세션 ID. 재시도는 새 Trial이라 새 세션입니다(이어 쓰지 않음)."""
    return str(uuid.uuid5(SESSION_NS, f"{envelope.tenant_id}/{envelope.trial_id}/{envelope.attempt}"))


def build_options(al: Allowlist, envelope: StateEnvelope, mcp_url: str, trial_token: str, route: LlmRoute,
                  config_dir: str, hooks: dict[str, Hook],
                  stderr: Callable[[str], None] | None = None, *, cli_env: dict[str, str] | None = None,
                  cwd: str | None = None) -> ClaudeAgentOptions:
    """cli_env: claude_subscription은 llm.subscription_env(구독 토큰), 게이트웨이는 비우면 llm.agent_env.
    cwd: 실행마다 새 빈 디렉터리(CLAUDE_CONFIG_DIR와 따로). 구독은 정액이라 USD 한도를 두지 않습니다."""
    granted = tuple(envelope.approved_scope.mcp_tools)
    denied_platform = tuple(mcp_tool(t) for t in PLATFORM_TOOLS if t not in granted)
    if route.provider == SUBSCRIPTION and cli_env is None:
        raise ValueError("claude_subscription needs cli_env from llm.subscription_env")
    return ClaudeAgentOptions(
        system_prompt=render_system_prompt(envelope, al),
        tools=[],
        allowed_tools=[mcp_tool(t) for t in granted],
        disallowed_tools=list(BUILTIN_DENY + denied_platform),
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers={MCP_SERVER: {"type": "http", "url": mcp_url,
                                  "headers": {"Authorization": f"Bearer {trial_token}"}}},
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[hooks["PreToolUse"]])],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[hooks["PostToolUse"]])],
            "UserPromptSubmit": [HookMatcher(matcher=None, hooks=[hooks["UserPromptSubmit"]])],
        },
        max_turns=al.limits.max_turns,
        # 구독 경로의 total_cost_usd는 API 가격 환산치이고 청구액이 아닙니다. 한도는 턴 · 시간으로 둡니다.
        max_budget_usd=None if route.provider == SUBSCRIPTION else min(al.limits.max_budget_usd, envelope.budget_usd),
        model=route.model,
        output_format={"type": "json_schema", "schema": output_schema(al.role)},
        session_id=session_id_for(envelope),
        cwd=cwd or config_dir,
        env=cli_env if cli_env is not None else agent_env(route, trial_token, config_dir),
        stderr=stderr,
    )
