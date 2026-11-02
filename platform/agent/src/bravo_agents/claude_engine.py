"""Claude Agent SDK 세션 하나: 격리 검사(init) → 실행 → 기록. run_agent와 실측 시험(canary)이 같은 함수를 씁니다.

init 메시지로 실행 전 격리를 확인하고, 하나라도 어긋나면 모델 결과를 쓰지 않고 멈춥니다(fail-closed).
- 도구: 승인된 `mcp__platform__*`와 `StructuredOutput`(output_format이 더하는 것)만. 그 밖이면 unexpected_tools
- MCP 서버: `platform` 하나, 상태 connected, 승인 도구가 모두 보여야 함. 아니면 unexpected_mcp_servers · mcp_unavailable
- 플러그인: CLI 내장(`builtin`)만. 운영자 ~/.claude 플러그인이 보이면 unexpected_plugins
- claude_subscription: apiKeySource가 none(API 키를 쓰지 않음)이어야 함. 아니면 unexpected_credentials
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from bravo_agents.errors import AgentRunError
from bravo_agents.llm import SUBSCRIPTION
from bravo_agents.options import MCP_SERVER, mcp_tool

# output_format을 켜면 CLI가 더하는 도구. 이것 말고 허용 목록 밖 도구가 init에 보이면 멈춥니다.
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def check_init(data: dict, granted: tuple[str, ...], provider: str) -> None:
    """SDK init 메시지로 격리를 확인합니다. 어긋나면 AgentRunError(인프라 코드)."""
    granted_names = {mcp_tool(t) for t in granted}
    tools = set(data.get("tools") or [])
    extra = tools - granted_names - {STRUCTURED_OUTPUT_TOOL}
    if extra:
        raise AgentRunError("unexpected_tools", ",".join(sorted(extra))[:500])
    servers = data.get("mcp_servers") or []
    names = sorted(str(s.get("name")) for s in servers)
    if names != [MCP_SERVER]:
        raise AgentRunError("unexpected_mcp_servers", ",".join(names)[:500])
    status = servers[0].get("status")
    if status != "connected":
        raise AgentRunError("mcp_unavailable", f"{MCP_SERVER} status {status}")
    missing = granted_names - tools
    if missing:
        raise AgentRunError("mcp_unavailable", f"granted tools not offered: {','.join(sorted(missing))}"[:500])
    foreign = [str(p.get("source") or p.get("name")) for p in data.get("plugins") or [] if p.get("path") != "builtin"]
    if foreign:
        raise AgentRunError("unexpected_plugins", ",".join(foreign)[:500])
    if provider == SUBSCRIPTION and data.get("apiKeySource") not in (None, "none"):
        raise AgentRunError("unexpected_credentials", f"apiKeySource {data.get('apiKeySource')}")


@dataclass
class ClaudeRun:
    init: dict = field(default_factory=dict)
    result: Any = None                                     # ResultMessage
    tool_uses: list[dict] = field(default_factory=list)    # 모델이 낸 도구 호출(이름, id, 입력)
    tool_errors: set[str] = field(default_factory=set)     # 오류로 끝난 tool_use_id
    transcript: list[dict] = field(default_factory=list)   # 모든 메시지(직렬화). canary가 표식이 없는지 봄

    def tool_summary(self, granted: tuple[str, ...]) -> dict:
        names = {mcp_tool(t) for t in granted} | {STRUCTURED_OUTPUT_TOOL}   # 구조화 출력 채널은 도구 목록 안
        outside = [u["name"] for u in self.tool_uses if u["name"] not in names]
        errored = [u["name"] for u in self.tool_uses if u["id"] in self.tool_errors]
        return {"calls": len(self.tool_uses), "outside_grant": outside, "errored": errored}


def _plain(msg: Any) -> dict:
    body = asdict(msg) if is_dataclass(msg) else {"repr": repr(msg)}
    return {"type": type(msg).__name__, **json.loads(json.dumps(body, default=str))}


async def run_claude(options: Any, prompt: str, granted: tuple[str, ...], provider: str,
                     timeout_s: float) -> ClaudeRun:
    """세션 하나를 끝까지 돌립니다. 격리 검사 실패 · 인증 실패 · 시간 초과는 AgentRunError."""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, SystemMessage, ToolResultBlock, \
        ToolUseBlock, UserMessage

    run = ClaudeRun()
    try:
        async with asyncio.timeout(timeout_s):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    run.transcript.append(_plain(msg))
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        run.init = dict(msg.data)
                        check_init(run.init, granted, provider)   # 어긋나면 여기서 세션을 끊음
                    elif isinstance(msg, AssistantMessage):
                        if msg.error == "authentication_failed":
                            raise AgentRunError("llm_auth_failed", "model provider refused the credential")
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                run.tool_uses.append({"id": block.id, "name": block.name, "input": block.input})
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock) and block.is_error:
                                run.tool_errors.add(block.tool_use_id)
                    elif isinstance(msg, ResultMessage):
                        run.result = msg
    except TimeoutError as e:
        raise AgentRunError("timeout", f"{timeout_s}s") from e
    if not run.init:
        raise AgentRunError("sdk_error", "no init message (isolation not verified)")
    return run


def usage_of(result: Any, provider: str) -> dict:
    """토큰 사용량. 구독 경로의 USD는 청구액이 아니라 API 가격 환산치라 따로 표시합니다."""
    usage = dict(result.usage or {}) if result is not None else {}
    keep = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                      "cache_read_input_tokens") if usage.get(k) is not None}
    if provider == SUBSCRIPTION:
        keep["cost_basis"] = "subscription (flat plan; USD not billed)"
        if result is not None and result.total_cost_usd is not None:
            keep["api_price_equivalent_usd"] = result.total_cost_usd
    return keep
