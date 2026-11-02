"""Model route for the agent worker (13 6 "LLM 경로"), resolved by ``bravo_agents.llm`` and failed closed.

The SDK options themselves are built by ``bravo_agents.options.build_options`` inside
``run_agent`` (no built-in tools, our MCP tool layer only, no setting sources, ``dontAsk``, turn
and budget caps from the role allowlist). The rehearsal side only decides whether a model route
exists and hands the worker the route's *non-secret* settings. The worker authenticates to the
LLM gateway with its Trial token; no model key or cloud credential is passed to it.

Route variables (13 6): every ``BRAVO_LLM_*``, ``BRAVO_MODEL_*``, ``BRAVO_BEDROCK_*`` and
``BRAVO_CODEX_*`` variable, plus the names listed in ``BRAVO_WORKER_ENV_PASSTHROUGH`` for a provider
that needs more (the provider is chosen with ``BRAVO_LLM_PROVIDER`` and validated by
``bravo_agents.llm.resolve_llm``; this module does not list providers). Bedrock Tokyo inference
profiles: ``jp.anthropic.claude-opus-5-5``, ``jp.anthropic.claude-haiku-5-5`` (ACTIVE in
ap-northeast-1, 2026-11-02; config only, no live call from this package).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

ROUTE_PREFIXES = ("BRAVO_LLM_", "BRAVO_MODEL_", "BRAVO_BEDROCK_", "BRAVO_CODEX_")
PASSTHROUGH_ENV = "BRAVO_WORKER_ENV_PASSTHROUGH"  # extra variable names a provider needs (comma-separated)
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class LLMUnconfigured(RuntimeError):
    reason = "llm_unconfigured"


def llm_env(tier: str = "opus", environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Route settings for the worker, after ``bravo_agents.llm.resolve_llm(tier)`` accepted them.

    Raises ``LLMUnconfigured`` when the provider, gateway URL, model ID for the tier or (Bedrock)
    region is missing, or when ``bravo_agents`` is not installed.
    """
    env = os.environ if environ is None else environ
    try:
        from bravo_agents.errors import AgentRunError
        from bravo_agents.llm import resolve_llm
    except ImportError as exc:
        raise LLMUnconfigured("bravo_agents not installed") from exc
    try:
        resolve_llm(tier, env)
    except AgentRunError as exc:
        raise LLMUnconfigured(exc.detail or exc.code) from exc
    names = {k for k in env if k.startswith(ROUTE_PREFIXES)}
    names |= {n.strip() for n in env.get(PASSTHROUGH_ENV, "").split(",") if NAME.match(n.strip())}
    names.discard(PASSTHROUGH_ENV)
    names.discard("BRAVO_LLM_GATEWAY_TOKEN")  # a gateway credential never goes to the worker
    return {k: str(env[k]) for k in sorted(names) if env.get(k)}
