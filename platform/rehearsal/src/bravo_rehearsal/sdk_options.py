"""Model route for the agent worker (13 6 "LLM 경로"), resolved by ``bravo_agents.llm`` and failed closed.

Every agent runs on an Agent SDK (team lead, 2026-11-02). Providers (``BRAVO_LLM_PROVIDER``, default
``claude_subscription``) are defined and validated by ``bravo_agents.llm.resolve_llm``:

- ``claude_subscription`` (default, demo): Claude Agent SDK with the team lead's subscription token, stored in
  the platform OpenBao (``secret/envs/platform/llm-claude``). The rehearsal service never reads it: per Trial it
  mints one single-use secret_id for the ``llm-claude`` AppRole and hands only that to the worker
  (``service.Orchestrator._worker_env``); the worker reads the token itself (``bravo_agents.llm_secret``).
- ``openai_agents`` (alternate engine): OpenAI Agents SDK, API key at ``secret/envs/platform/llm-openai``, same
  delivery through the ``llm-openai`` AppRole.
- ``anthropic_gateway`` / ``bedrock_gateway`` (production target): an LLM gateway holding the platform key; the
  worker authenticates with its Trial token. Bedrock Tokyo inference profiles: ``jp.anthropic.claude-opus-5-5``,
  ``jp.anthropic.claude-haiku-5-5`` (config only, no live call from this package).

The worker gets exactly ``ROUTE_VARS`` (non-secret settings), never a model key or a gateway token.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Exact names only (audit 32).
ROUTE_VARS = ("BRAVO_LLM_PROVIDER", "BRAVO_LLM_GATEWAY_URL", "BRAVO_MODEL_OPUS", "BRAVO_MODEL_HAIKU",
              "BRAVO_BEDROCK_REGION")


class LLMUnconfigured(RuntimeError):
    reason = "llm_unconfigured"


def provider(environ: Mapping[str, str] | None = None) -> str:
    try:
        from bravo_agents.llm import provider_name
    except ImportError as exc:
        raise LLMUnconfigured("bravo_agents not installed") from exc
    return provider_name(os.environ if environ is None else environ)


def vault_approle(environ: Mapping[str, str] | None = None) -> str | None:
    """The OpenBao AppRole the worker needs for its model credential, or None for gateway providers."""
    try:
        from bravo_agents.llm_secret import LLM_SECRETS
    except ImportError:
        return None
    secret = LLM_SECRETS.get(provider(environ))
    return secret.approle if secret else None


def check_route(tier: str, env: Mapping[str, str]) -> None:
    """The same resolution ``bravo_agents.run_agent`` uses."""
    try:
        from bravo_agents.errors import AgentRunError
        from bravo_agents.llm import resolve_llm
    except ImportError as exc:
        raise LLMUnconfigured("bravo_agents not installed") from exc
    try:
        resolve_llm(tier, env)
    except AgentRunError as exc:
        raise LLMUnconfigured(exc.detail or exc.code) from exc


def llm_env(tier: str = "opus", environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Route settings for the worker, after ``bravo_agents.llm.resolve_llm(tier)`` accepted them."""
    env = os.environ if environ is None else environ
    check_route(tier, env)
    return {k: str(env[k]) for k in ROUTE_VARS if env.get(k)}
