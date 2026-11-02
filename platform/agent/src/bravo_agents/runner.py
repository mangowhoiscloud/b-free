"""run_agent: 역할 하나를 Agent SDK 세션 하나로 돌려 검증된 제안서를 서버에 내고 돌려줍니다.

엔진은 BRAVO_LLM_PROVIDER로 고릅니다(llm.py). claude_subscription(기본) · 게이트웨이는 Claude Agent SDK,
openai_agents는 OpenAI Agents SDK. 어느 엔진이든 같은 봉투 · 도구 계층 · 출력 모델 · 검증 · 제출 경로를 씁니다.

모델의 결과 채널은 구조화 출력 하나입니다. 모델에게는 제출 도구(plan_propose)가 없습니다.
이 워커(결정적 코드)가 Pydantic으로 검증한 뒤 도구 계층의 plan_propose로 냅니다. 서버가 다시 검증하고,
서버에 들어간 내용만 산출물의 정본입니다(platform/rehearsal worker 계약).
"""
import asyncio
import hashlib
import json
import logging
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
from bravo_agents.llm import OPENAI, SUBSCRIPTION, LlmRoute, resolve_llm, subscription_env
from bravo_agents.options import build_options
from bravo_agents.schemas import OUTPUT_MODELS, ProposalBase, referenced_evidence

log = logging.getLogger(__name__)


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
    usage: dict | None = None   # 토큰 사용량. 구독 경로는 정액이라 total_cost_usd=None(환산치는 usage에만)


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
    route = resolve_llm(al.model_tier)                       # 없으면 llm_unconfigured
    sink = evidence or evidence_sink_from_env(trial_token)   # 없으면 evidence_unconfigured
    budget_s = min(al.limits.timeout_s, remaining_seconds(envelope, datetime.now(timezone.utc)))
    if budget_s <= 0:
        raise AgentRunError("timeout", "deadline already passed")
    if route.provider == OPENAI:
        return await _run_openai(role, al, envelope, mcp_url, trial_token, route, sink, submit, budget_s)
    return await _run_claude(role, al, envelope, mcp_url, trial_token, route, sink, submit, budget_s)


async def _secret(provider: str) -> str:
    """플랫폼 OpenBao에서 한 번 쓰는 AppRole로 모델 자격을 읽습니다(없으면 llm_unconfigured)."""
    from bravo_agents.llm_secret import fetch_llm_secret

    return await asyncio.to_thread(fetch_llm_secret, provider)


async def _run_claude(role, al, envelope: StateEnvelope, mcp_url: str, trial_token: str, route: LlmRoute,
                      sink: EvidenceSink, submit: bool, budget_s: int) -> Proposal:
    from bravo_agents.claude_engine import run_claude, usage_of

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as e:  # pragma: no cover - 설치 오류
        raise AgentRunError("sdk_unavailable", str(e)) from e
    config_dir = tempfile.mkdtemp(prefix=f"bravo-{envelope.trial_id}-cfg-")
    work_dir = tempfile.mkdtemp(prefix=f"bravo-{envelope.trial_id}-cwd-")   # 빈 cwd
    try:
        cli_env = None
        if route.provider == SUBSCRIPTION:
            cli_env = subscription_env(await _secret(route.provider), config_dir)   # 값은 CLI 환경에만
        options = build_options(al, envelope, mcp_url, trial_token, route, config_dir, make_hooks(envelope, sink),
                                stderr=lambda line: log.info("cli: %s", line), cli_env=cli_env, cwd=work_dir)
        cli_env = None
        granted = tuple(envelope.approved_scope.mcp_tools)
        run = await run_claude(options, render_task_prompt(envelope), granted, route.provider, budget_s)
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)       # 세션 설정 · 캐시를 남기지 않음
        shutil.rmtree(work_dir, ignore_errors=True)
    log.info("claude run provider=%s tools=%s", route.provider, run.tool_summary(granted))
    result = run.result
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
    cost = None if route.provider == SUBSCRIPTION else result.total_cost_usd
    return Proposal(role, out, digest(out), result.session_id, route.model, result.num_turns, cost, route.provider,
                    {**usage_of(result, route.provider), "tools": run.tool_summary(granted)})


async def _run_openai(role, al, envelope: StateEnvelope, mcp_url: str, trial_token: str, route: LlmRoute,
                      sink: EvidenceSink, submit: bool, budget_s: int) -> Proposal:
    from bravo_agents.openai_engine import run_openai

    try:
        import agents  # noqa: F401
    except ImportError as e:
        raise AgentRunError("sdk_unavailable", "openai-agents not installed (pip extra 'openai')") from e
    granted = tuple(envelope.approved_scope.mcp_tools)
    base = {"trial_id": envelope.trial_id, "plan_id": envelope.plan_id, "attempt": envelope.attempt, "role": role}
    run = await run_openai(instructions=render_system_prompt(envelope, al), prompt=render_task_prompt(envelope),
                           model=route.model, output_model=OUTPUT_MODELS[role], mcp_url=mcp_url,
                           trial_token=trial_token, granted=granted, api_key=await _secret(route.provider),
                           max_turns=al.limits.max_turns, timeout_s=budget_s, sink=sink, base_event=base)
    raw = run.output.model_dump(mode="json") if isinstance(run.output, ProposalBase) else run.output
    out = validate_output(role, raw, envelope)
    if submit:
        await submit_proposal(mcp_url, trial_token, out)
    return Proposal(role, out, digest(out), run.response_id or "", route.model, run.requests, None, route.provider,
                    {**run.usage, "strict_schema": run.strict_schema,
                     "tools": {"offered": run.tool_names, "calls": run.tool_calls}})
