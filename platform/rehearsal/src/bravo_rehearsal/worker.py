"""Agent worker entrypoint: ``python -m bravo_rehearsal.worker`` (one process per Trial).

Started by ``harbor_agent.RehearsalAgent`` with an environment built from scratch: the Trial
token, the tool-layer URL, the evidence URL, the state envelope (``bravo_agents.StateEnvelope``),
the role, the allowlist directory, the model route settings and, for ``claude_subscription`` /
``openai_agents``, a one-use OpenBao secret_id (no model key, no kubeconfig, no cloud or DB
credentials). The loop is ``bravo_agents.run_agent(role, envelope, mcp_url,
trial_token)``; it validates its structured output and submits it with ``plan_propose``. Its
return value is not the artifact: the server-side ``plan_propose`` record is. Exit codes map to
Trial reasons; for 23 the last stderr line is ``agent_error:<code>`` (``AgentRunError.code``):

    0  agent finished           20 llm_unconfigured     21 agent_runtime_unavailable
    22 envelope_invalid         23 agent_error
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys

EXIT_REASONS = {20: "llm_unconfigured", 21: "agent_runtime_unavailable", 22: "envelope_invalid", 23: "agent_error"}


def main() -> int:
    role = os.environ.get("BRAVO_AGENT_ROLE", "")
    try:
        from bravo_agents import load_allowlist
        from bravo_agents.errors import AgentRunError
    except ImportError:
        print("agent_runtime_unavailable", file=sys.stderr)
        return 21
    from .sdk_options import LLMUnconfigured, check_route

    try:
        tier = load_allowlist(role).model_tier
    except AgentRunError:
        tier = "opus"  # the allowlist problem is reported by run_agent; the model route is checked first
    try:
        # Fail closed before anything else: never run without a model route and, for the OpenBao
        # providers, the one-use credential delivery (bravo_agents.llm_secret reads it at run time).
        check_route(tier, os.environ)
        from bravo_agents.llm import provider_name
        from bravo_agents.llm_secret import delivery_problem

        if delivery_problem(provider_name(os.environ), os.environ):
            raise LLMUnconfigured("openbao_delivery_missing")
    except LLMUnconfigured:
        print("llm_unconfigured", file=sys.stderr)
        return 20
    try:
        envelope = json.loads(os.environ["BRAVO_TRIAL_ENVELOPE"])
        mcp_url = os.environ["BRAVO_TOOL_LAYER_URL"]
        token = os.environ["BRAVO_TRIAL_TOKEN"]
    except (KeyError, ValueError):
        print("envelope_invalid", file=sys.stderr)
        return 22
    from bravo_agents import run_agent

    try:
        result = run_agent(role, envelope, mcp_url, token)
        if inspect.isawaitable(result):
            result = asyncio.run(result)  # type: ignore[arg-type]
    except AgentRunError as exc:
        print(f"agent_error:{exc.code}", file=sys.stderr)
        return 23
    except Exception as exc:  # the Trial records the reason; details go to stderr (worker log)
        print(f"agent_error:{type(exc).__name__}", file=sys.stderr)
        return 23
    usage = {"provider": getattr(result, "provider", None), "model": getattr(result, "model", None),
             "num_turns": getattr(result, "num_turns", None), "total_cost_usd": getattr(result, "total_cost_usd", None),
             "tokens": getattr(result, "usage", None), "digest": getattr(result, "digest", None)}
    print("agent_result:" + json.dumps(usage, sort_keys=True, default=str))  # stdout: usage summary for the trajectory
    return 0


if __name__ == "__main__":
    sys.exit(main())
