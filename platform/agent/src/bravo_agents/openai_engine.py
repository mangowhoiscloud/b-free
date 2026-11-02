"""openai_agents: OpenAI Agents SDK(openai-agents==0.20.0)를 같은 계약 뒤의 두 번째 엔진으로 씁니다(Beyond Lock-In).

계약은 Claude 경로와 같습니다: 봉투 → 시스템 프롬프트, 도구는 플랫폼 MCP 도구 계층의 승인 도구만, 결과는 역할
Pydantic 모델(output_type) 하나, 워커가 다시 검증해 plan_propose로 제출.
- 도구: Agent(tools=[], handoffs=[]), mcp_servers=[platform](Streamable HTTP, Trial 토큰 헤더, tool_filter=승인 도구).
  실행 전 get_all_tools로 모델에게 갈 도구 목록이 승인 도구와 정확히 같은지 확인합니다. 다르면 멈춤.
- 자격: 플랫폼 OpenBao(secret/envs/platform/llm-openai)에서 실행 때 읽어 이 클라이언트에만 넘깁니다(llm_secret).
  환경 변수(OPENAI_API_KEY, OPENAI_BASE_URL)를 읽지 않고, 추적(tracing) 전송은 끕니다. 모드는 둘입니다.
  - api_key(운영 대안): 주소 api.openai.com 고정, Runner.run.
  - chatgpt(데모 전용, 팀장 지시 2026-11-02 "openai_agents도 계정으로 연결해. 계정은 플랫폼에서 관리하고"):
    팀장 ChatGPT 계정의 Codex CLI 로그인 세션. OpenAI가 Agents SDK용으로 문서화하지 않은 ChatGPT Codex
    백엔드(chatgpt_auth.CODEX_BASE_URL, Responses 호환)를 AsyncOpenAI의 base_url로 두고 OpenAIResponsesModel에
    넘깁니다. 백엔드 요구(Codex 소스 기준): store=false, 스트리밍(Runner.run_streamed), instructions,
    include=reasoning.encrypted_content, 다음 턴 입력에서 reasoning item ID 빼기. 계정 헤더는 ChatGPT-Account-ID.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from bravo_agents.chatgpt_auth import CODEX_BASE_URL, ChatGPTSession, backend_headers
from bravo_agents.errors import AgentRunError
from bravo_agents.hooks import EvidenceSink
from bravo_agents.options import MCP_SERVER

OPENAI_BASE_URL = "https://api.openai.com/v1"
log = logging.getLogger(__name__)
_TOKENISH = re.compile(r"[A-Za-z0-9_\-.]{40,}")   # 오류 메시지에서 토큰처럼 긴 문자열은 지움


def output_schema_for(model: type) -> Any:
    """엄격 스키마가 되면 엄격으로, 자유 형식 객체가 있는 역할(governance, ci)은 비엄격. 어느 쪽이든 Pydantic이 다시 검증."""
    from agents import AgentOutputSchema
    from agents.exceptions import UserError

    try:
        return AgentOutputSchema(model, strict_json_schema=True)
    except UserError:
        return AgentOutputSchema(model, strict_json_schema=False)


def make_server(mcp_url: str, trial_token: str, granted: tuple[str, ...]) -> Any:
    from agents.mcp import MCPServerStreamableHttp, create_static_tool_filter

    return MCPServerStreamableHttp(
        params={"url": mcp_url, "headers": {"Authorization": f"Bearer {trial_token}"}, "timeout": 30,
                "sse_read_timeout": 120},
        name=MCP_SERVER, cache_tools_list=True, client_session_timeout_seconds=120,
        tool_filter=create_static_tool_filter(allowed_tool_names=list(granted)),
    )


def make_run_config(api_key: str) -> Any:
    from agents import RunConfig
    from agents.models.openai_provider import OpenAIProvider
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=OPENAI_BASE_URL, max_retries=2)
    return RunConfig(model_provider=OpenAIProvider(openai_client=client, use_responses=True), tracing_disabled=True,
                     trace_include_sensitive_data=False)


def make_chatgpt_run_config(model: str, session: ChatGPTSession, session_id: str) -> Any:
    """ChatGPT 계정 모드. Codex 백엔드 + 접근 토큰 + 계정 헤더를 가진 클라이언트를 OpenAIResponsesModel에 넘깁니다."""
    from agents import ModelSettings, RunConfig
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=session.access_token, base_url=CODEX_BASE_URL, max_retries=2,
                         default_headers=backend_headers(session, session_id))
    return RunConfig(model=OpenAIResponsesModel(model=model, openai_client=client),
                     model_settings=ModelSettings(store=False, response_include=["reasoning.encrypted_content"]),
                     reasoning_item_id_policy="omit", tracing_disabled=True, trace_include_sensitive_data=False)


async def check_tools(agent: Any, granted: tuple[str, ...]) -> list[str]:
    """모델에게 갈 도구 이름. 승인 도구와 정확히 같지 않으면 AgentRunError."""
    from agents import RunContextWrapper

    names = sorted(t.name for t in await agent.get_all_tools(RunContextWrapper(context=None)))
    extra = sorted(set(names) - set(granted))
    if extra or agent.handoffs:
        raise AgentRunError("unexpected_tools", ",".join(extra + [str(h) for h in agent.handoffs])[:500])
    missing = sorted(set(granted) - set(names))
    if missing:
        raise AgentRunError("mcp_unavailable", f"granted tools not offered: {','.join(missing)}"[:500])
    return names


@dataclass
class OpenAIRun:
    output: Any = None
    tool_names: list[str] = field(default_factory=list)    # 실행 전 확인한 도구 목록
    tool_calls: list[str] = field(default_factory=list)    # 실행 중 모델이 부른 도구
    usage: dict = field(default_factory=dict)
    response_id: str | None = None
    requests: int = 0
    strict_schema: bool = True
    mode: str = "api_key"
    transcript: list[dict] = field(default_factory=list)   # 모든 항목 · 응답(직렬화). canary가 표식이 없는지 봄


def _plain(obj: Any) -> Any:
    raw = getattr(obj, "raw_item", obj)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    return json.loads(json.dumps(raw, default=str))


def _error_detail(e: Exception) -> str:
    """형식 이름과 HTTP 상태, 서버 메시지(토큰처럼 긴 문자열은 지움)."""
    status = getattr(e, "status_code", None)
    msg = _TOKENISH.sub("[redacted]", str(getattr(e, "message", "") or ""))[:240]
    return " ".join(x for x in (type(e).__name__, str(status) if status else "", msg) if x)


def _evidence_hooks(sink: EvidenceSink, base: dict) -> Any:
    from agents import RunHooks

    class Hooks(RunHooks):
        def __init__(self) -> None:
            self.seq = 0

        async def _emit(self, kind: str, tool: Any) -> None:
            self.seq += 1
            await sink.emit({**base, "seq": self.seq, "kind": kind,
                             "tool_name": f"mcp__{MCP_SERVER}__{getattr(tool, 'name', tool)}",
                             "provider": "openai_agents"})

        async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
            await self._emit("tool_request", tool)

        async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: object) -> None:
            await self._emit("tool_result", tool)

    return Hooks()


async def run_openai(*, instructions: str, prompt: str, model: str, output_model: type, mcp_url: str,
                     trial_token: str, granted: tuple[str, ...], credential: Any, max_turns: int, timeout_s: float,
                     sink: EvidenceSink, base_event: dict) -> OpenAIRun:
    """credential은 llm_secret.OpenAICredential(api_key 또는 chatgpt)."""
    try:
        from agents import Agent, Runner, set_tracing_disabled
        from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError
        from agents.items import ToolCallItem
    except ImportError as e:
        raise AgentRunError("sdk_unavailable", "openai-agents not installed (pip extra 'openai')") from e
    set_tracing_disabled(True)       # 추적 내보내기는 환경의 키를 쓸 수 있어 프로세스 전체에서 끔
    schema = output_schema_for(output_model)
    run = OpenAIRun(strict_schema=schema.is_strict_json_schema(), mode=credential.mode)
    if credential.mode == "chatgpt":
        run_config = make_chatgpt_run_config(model, credential.session, str(uuid.uuid4()))
    elif credential.mode == "api_key":
        run_config = make_run_config(credential.api_key)
    else:
        raise AgentRunError("llm_unconfigured", f"unknown openai credential mode {credential.mode}")
    server = make_server(mcp_url, trial_token, granted)
    try:
        async with asyncio.timeout(timeout_s):
            async with server:
                agent = Agent(name=f"bravo-{base_event.get('role')}", instructions=instructions, model=model,
                              tools=[], handoffs=[], mcp_servers=[server], output_type=schema)
                run.tool_names = await check_tools(agent, granted)
                log.info("openai_agents mode=%s model=%s tools offered=%s", credential.mode, model, run.tool_names)
                hooks = _evidence_hooks(sink, base_event)
                if credential.mode == "chatgpt":     # Codex 백엔드는 스트리밍만 받음
                    result = Runner.run_streamed(agent, prompt, max_turns=max_turns, run_config=run_config,
                                                 hooks=hooks)
                    async for _ in result.stream_events():
                        pass
                else:
                    result = await Runner.run(agent, prompt, max_turns=max_turns, run_config=run_config, hooks=hooks)
    except TimeoutError as e:
        raise AgentRunError("timeout", f"{timeout_s}s") from e
    except MaxTurnsExceeded as e:
        raise AgentRunError("max_turns") from e
    except ModelBehaviorError as e:
        raise AgentRunError("schema_invalid", str(e)[:500]) from e
    except AgentRunError:
        raise
    except AgentsException as e:
        raise AgentRunError("sdk_error", f"{type(e).__name__}: {str(e)[:300]}") from e
    except Exception as e:   # openai.AuthenticationError 등. 인증 오류는 형식 이름만, 나머지는 서버 메시지(토큰 지움)
        if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
            raise AgentRunError("llm_auth_failed", type(e).__name__) from e
        raise AgentRunError("sdk_error", _error_detail(e)) from e
    for item in result.new_items:
        if isinstance(item, ToolCallItem):
            run.tool_calls.append(str(getattr(item.raw_item, "name", None) or getattr(item.raw_item, "type", "")))
    outside = sorted(set(run.tool_calls) - set(granted))
    if outside:
        raise AgentRunError("unexpected_tools", ",".join(outside)[:500])
    run.transcript = [_plain(i) for i in result.new_items] + [_plain(o) for r in result.raw_responses for o in r.output]
    u = result.context_wrapper.usage
    run.usage = {"requests": u.requests, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "total_tokens": u.total_tokens,
                 "cached_input_tokens": getattr(u.input_tokens_details, "cached_tokens", None),
                 "reasoning_tokens": getattr(u.output_tokens_details, "reasoning_tokens", None),
                 "cost_basis": ("ChatGPT plan (flat; no USD billed)" if credential.mode == "chatgpt"
                                else "OpenAI API key (per token)")}
    run.requests = u.requests
    run.response_id = result.last_response_id
    run.output = result.final_output
    return run
