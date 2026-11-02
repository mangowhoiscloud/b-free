"""run_agent: 역할 하나를 SDK 세션 하나로 돌려 검증된 제안서를 서버에 내고 돌려줍니다.

모델의 결과 채널은 구조화 출력 하나입니다. 모델에게는 제출 도구(plan_propose)가 없습니다.
이 워커(결정적 코드)가 Pydantic으로 검증한 뒤 도구 계층의 plan_propose로 냅니다. 서버가 다시 검증하고,
서버에 들어간 내용만 산출물의 정본입니다(platform/rehearsal worker 계약).
"""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import ValidationError

from bravo_agents.allowlist import load_allowlist
from bravo_agents.envelope import (StateEnvelope, check_scope, remaining_seconds, render_system_prompt,
                                   render_task_prompt)
from bravo_agents.errors import AgentRunError
from bravo_agents.hooks import EvidenceSink, evidence_sink_from_env, make_hooks
from bravo_agents.llm import PROVIDER_ENV, resolve_llm
from bravo_agents.options import build_options, mcp_tool
from bravo_agents.schemas import OUTPUT_MODELS, ProposalBase, output_schema, referenced_evidence

log = logging.getLogger(__name__)
# output_format을 켜면 CLI가 더하는 도구. 이것 말고 허용 목록 밖 도구가 init에 보이면 멈춥니다.
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


@dataclass(frozen=True)
class Proposal:
    role: str
    output: ProposalBase
    digest: str              # 정규화 JSON의 sha256. 승인 · 검증 결속에 씀
    session_id: str
    model: str
    num_turns: int
    total_cost_usd: float | None
    provider: str = ""
    usage: dict | None = None   # 토큰 사용량(codex_chatgpt: ThreadTokenUsage). 비용은 플랜 정액이라 None


def validate_output(role: str, raw, envelope: StateEnvelope) -> ProposalBase:
    """스키마 + 봉투 대조. Pod 안 검사는 빠른 반려용이고, 도구 계층이 같은 검사를 다시 합니다."""
    if raw is None:
        raise AgentRunError("no_structured_output")
    try:
        out = OUTPUT_MODELS[role].model_validate(raw)
    except ValidationError as e:
        raise AgentRunError("schema_invalid", str(e.errors(include_url=False))[:2000]) from e
    if (out.plan_id, out.trial_id, out.attempt) != (envelope.plan_id, envelope.trial_id, envelope.attempt):
        raise AgentRunError("binding_mismatch", "plan_id/trial_id/attempt differ from envelope")
    unknown = referenced_evidence(out) - envelope.evidence_ids()
    if unknown:
        raise AgentRunError("ref_invalid", f"unknown evidence ids {sorted(unknown)[:10]}")
    return out


def digest(out: ProposalBase) -> str:
    body = json.dumps(out.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


async def submit_proposal(mcp_url: str, trial_token: str, out: ProposalBase) -> None:
    """검증된 제안서를 도구 계층 MCP의 plan_propose로 냅니다. 거부되면 proposal_rejected."""
    import httpx2  # mcp 2.x의 HTTP 클라이언트
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {trial_token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=30.0) as http:
        async with streamable_http_client(mcp_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 서버는 plan_propose를 tools/list에 내놓지 않고, 받은 뒤 Trial을 잠급니다(이후 요청 403).
                # mcp 2.x는 목록에 없는 도구의 결과를 검증하려 list_tools를 다시 부르므로 미리 "스키마 없음"으로 둡니다.
                session._tool_output_schemas.setdefault("plan_propose", None)
                res = await session.call_tool("plan_propose", {"proposal": out.model_dump(mode="json")})
    # mcp 2.x는 is_error(스네이크 표기). 이전 판의 isError도 받고, 둘 다 없으면 거부로 봅니다.
    if getattr(res, "is_error", getattr(res, "isError", True)):
        text = " ".join(getattr(c, "text", "") for c in getattr(res, "content", []) or [])[:500]
        raise AgentRunError("proposal_rejected", text)


async def run_agent(role: str, envelope: StateEnvelope | dict, mcp_url: str, trial_token: str, *,
                    evidence: EvidenceSink | None = None, submit: bool = True) -> Proposal:
    if not isinstance(envelope, StateEnvelope):
        try:
            envelope = StateEnvelope.model_validate(envelope)
        except ValidationError as e:
            raise AgentRunError("envelope_invalid", str(e.errors(include_url=False))[:1000]) from e
    al = load_allowlist(role)
    try:
        check_scope(envelope, al)
    except ValueError as e:
        raise AgentRunError(str(e)) from e
    if os.environ.get(PROVIDER_ENV) == "codex_chatgpt":     # 파일럿 · 데모 전용(13-agents.md)
        return await _run_codex(role, al, envelope, mcp_url, trial_token, evidence, submit)
    route = resolve_llm(al.model_tier)                       # 없으면 llm_unconfigured
    sink = evidence or evidence_sink_from_env(trial_token)   # 없으면 evidence_unconfigured
    try:
        from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage
    except ImportError as e:  # pragma: no cover - 설치 오류
        raise AgentRunError("sdk_unavailable", str(e)) from e

    now = datetime.now(timezone.utc)
    budget_s = min(al.limits.timeout_s, remaining_seconds(envelope, now))
    if budget_s <= 0:
        raise AgentRunError("timeout", "deadline already passed")
    config_dir = tempfile.mkdtemp(prefix=f"bravo-{envelope.trial_id}-")
    options = build_options(al, envelope, mcp_url, trial_token, route, config_dir, make_hooks(envelope, sink),
                            stderr=lambda line: log.info("cli: %s", line))
    expected = {mcp_tool(t) for t in envelope.approved_scope.mcp_tools} | {STRUCTURED_OUTPUT_TOOL}
    result = None
    try:
        async with asyncio.timeout(budget_s):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(render_task_prompt(envelope))
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        extra = set(msg.data.get("tools") or []) - expected
                        if extra:
                            raise AgentRunError("unexpected_tools", ",".join(sorted(extra)))
                    elif isinstance(msg, ResultMessage):
                        result = msg
    except TimeoutError as e:
        raise AgentRunError("timeout", f"{budget_s}s") from e
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)       # 세션 설정 · 캐시를 남기지 않음
    if result is None:
        raise AgentRunError("sdk_error", "no ResultMessage")
    if result.subtype == "error_max_turns":
        raise AgentRunError("max_turns")
    if result.subtype == "error_max_budget_usd":
        raise AgentRunError("max_budget")
    if result.subtype != "success" or result.is_error:
        raise AgentRunError("no_structured_output", result.subtype)
    out = validate_output(role, result.structured_output, envelope)
    if submit:
        await submit_proposal(mcp_url, trial_token, out)
    return Proposal(role, out, digest(out), result.session_id, route.model, result.num_turns,
                    result.total_cost_usd, route.provider)


async def _run_codex(role, al, envelope: StateEnvelope, mcp_url: str, trial_token: str,
                     evidence: EvidenceSink | None, submit: bool) -> Proposal:
    """codex_chatgpt: 격리 사전 검사를 통과해야만 실행합니다. 같은 검증 · 제출 경로를 씁니다."""
    from bravo_agents import codex_chatgpt as cx

    route = cx.resolve_codex()                               # 없으면 llm_unconfigured
    sink = evidence or evidence_sink_from_env(trial_token)
    try:
        import openai_codex  # noqa: F401
    except ImportError as e:
        raise AgentRunError("sdk_unavailable", "openai-codex not installed (pip extra 'codex')") from e
    budget_s = min(al.limits.timeout_s, remaining_seconds(envelope, datetime.now(timezone.utc)))
    if budget_s <= 0:
        raise AgentRunError("timeout", "deadline already passed")
    granted = tuple(envelope.approved_scope.mcp_tools)
    instructions = render_system_prompt(envelope, al)
    report = await cx.isolation_preflight(route, mcp_url, trial_token, granted, timeout_s=min(60, budget_s))
    log.info("codex isolation preflight tools=%s extra=%s", report.tool_names, report.extra)
    if not report.ok:
        raise AgentRunError("codex_isolation_failed", ",".join(report.extra)[:500])
    turn = await cx.run_codex_turn(route, mcp_url, trial_token, granted, instructions, render_task_prompt(envelope),
                                   cx.strict_schema(output_schema(role)), al.limits.max_turns, budget_s)
    bad = cx.check_items(turn.items, granted)
    if bad:
        raise AgentRunError("unexpected_tools", ",".join(bad)[:500])
    seq = 0
    for it in turn.items:
        if it.get("type") == "mcpToolCall":
            seq += 1
            await sink.emit({"trial_id": envelope.trial_id, "plan_id": envelope.plan_id, "attempt": envelope.attempt,
                             "role": role, "seq": seq, "kind": "tool_call", "tool_name": f"mcp__platform__{it.get('tool')}",
                             "tool_input": it.get("arguments"), "status": it.get("status"),
                             "provider": "codex_chatgpt"})
    if turn.status != "completed":
        raise AgentRunError("no_structured_output", f"turn {turn.status}: {turn.error or ''}"[:500])
    try:
        raw = json.loads(turn.final_response) if turn.final_response else None
    except ValueError as e:
        raise AgentRunError("schema_invalid", "final response is not JSON") from e
    out = validate_output(role, raw, envelope)
    if submit:
        await submit_proposal(mcp_url, trial_token, out)
    return Proposal(role, out, digest(out), turn.thread_id, route.model, seq + 1, None, route.provider, turn.usage)
