"""Harbor 0.25.0 agent: runs our agent worker for one Trial and records its trajectory.

import_path: ``bravo_rehearsal.harbor_agent:RehearsalAgent`` (agent kwarg ``trial_id``).

Harbor calls ``run()`` in the rehearsal service process. The model loop does not run here and
not in the sandbox: ``run()`` starts the worker process (``python -m bravo_rehearsal.worker`` →
``bravo_agents.run_agent``) with an environment built from scratch (Trial token, tool-layer
URL, evidence URL, ``StateEnvelope``, role, model route settings; no model key, no kubeconfig)
and waits. The worker acts only through the MCP tool layer, which reaches this Trial's sandbox
Pod in the Harbor environment with the namespace-bound exec identity. The only output channel
is ``plan_propose`` (called by the worker, never by the model): the frozen artifact on the
service's Trial record. Nothing is applied to the cluster from the proposal; no role gets
kubectl (13 4). The ATIF trajectory is written from the server-side tool-call record.

Failures are raised, never papered over. Harbor records the exception and skips the verifier:

- ``AgentRefused``: a precondition does not hold (``llm_unconfigured``, ``role_allowlist_missing``,
  ``sandbox_not_harbor_namespace``, ...). The worker is not started.
- ``AgentWorkerFailed``: the worker exited with a reason code or the Trial deadline passed
  without a proposal.
- ``NoProposal``: the worker finished without an accepted ``plan_propose``, or ``run_agent`` ended
  with an agent-side code (``agent_failed:<code>``: schema, binding, limits, rejected proposal).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.agent import Agent as AtifAgent
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory

from . import harbor_binding
from .harbor_env import RehearsalNamespaceEnvironment
from .sdk_options import LLMUnconfigured, llm_env
from .trials import Trial
from .worker import EXIT_REASONS

AGENT_NAME = "bravo-rehearsal"
WORKER_EXIT_GRACE_S = 15.0


class AgentFailure(RuntimeError):
    """``str(exc)`` is the Trial reason (Harbor stores it as ``exception_message``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AgentRefused(AgentFailure):
    pass


class AgentWorkerFailed(AgentFailure):
    pass


class NoProposal(AgentFailure):
    pass


def trajectory(trial: Trial, version: str) -> Trajectory:
    """ATIF from the tool layer's server-side record (custom agents write their own trajectory)."""
    steps = [
        Step(
            step_id=i,
            timestamp=ev["at"],
            source="agent",
            message=f"{ev['tool']} {'allowed' if ev['allowed'] else 'blocked'} rule={ev['rule']}",
            tool_calls=[ToolCall(tool_call_id=f"{trial.trial_id}-{ev['seq']}", function_name=ev["tool"],
                                 arguments={"args_sha256": ev["args_sha256"]},
                                 extra={"allowed": ev["allowed"], "rule": ev["rule"], "result": ev["result"]})],
        )
        for i, ev in enumerate(trial.tool_events, start=1)
    ]
    return Trajectory(session_id=trial.trial_id, agent=AtifAgent(name=AGENT_NAME, version=version), steps=steps,
                      extra={"artifact_digest": trial.artifact_digest, "role": trial.role, "attempt": trial.attempt})


@dataclass
class WorkerRun:
    returncode: int | None  # None: still running when the Trial deadline or the proposal ended the wait
    waited_out: bool
    error_code: str | None = None  # AgentRunError.code from the worker's stderr (exit 23)
    usage: dict[str, Any] = field(default_factory=dict)  # model, num_turns, total_cost_usd from stdout


def check_preconditions(trial: Trial) -> None:
    try:
        llm_env(trial.allowlist.model_tier)
    except LLMUnconfigured as exc:
        raise AgentRefused("llm_unconfigured") from exc
    if trial.allowlist.problem:
        raise AgentRefused(trial.allowlist.problem)


def _last(path: Path, prefix: str) -> str | None:
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith(prefix)]
    except OSError:
        return None
    return lines[-1][len(prefix):].strip() if lines else None


async def run_worker(binding: harbor_binding.Binding, log_dir: Path) -> WorkerRun:
    """Start the worker process with the binding's clean environment and wait for it or for the proposal.

    Raw worker output stays on the platform (``log_dir``): Harbor uploads ``agent/`` back into the
    sandbox after the run, so it is kept out of there (the trajectory carries hashes only).
    """
    trial = binding.trial
    config_dir = tempfile.mkdtemp(prefix="bravo-agent-")
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / "worker.stdout.log", log_dir / "worker.stderr.log"
    out, err = out_path.open("wb"), err_path.open("wb")
    proc = None
    try:
        env = binding.worker_env(config_dir)
        trial.phase = "agent_running"
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "bravo_rehearsal.worker", env=env,
                                                    cwd=config_dir, stdout=out, stderr=err)
        waiter = asyncio.create_task(proc.wait())
        proposed = asyncio.create_task(trial.proposed.wait())
        done, _ = await asyncio.wait({waiter, proposed}, timeout=max(1.0, trial.deadline - time.time()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done and proposed in done:
            # plan_propose accepted: give the worker a moment to finish cleanly and report its usage.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), WORKER_EXIT_GRACE_S)
        proposed.cancel()
        rc = proc.returncode
        waited_out = not done
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        out.close()
        err.close()
        shutil.rmtree(config_dir, ignore_errors=True)
    usage_line = _last(out_path, "agent_result:")
    try:
        usage = json.loads(usage_line) if usage_line else {}
    except ValueError:
        usage = {}
    return WorkerRun(rc, waited_out, _last(err_path, "agent_error:"), usage if isinstance(usage, dict) else {})


def raise_for_outcome(trial: Trial, run: WorkerRun | None) -> None:
    """No artifact: the worker's reason. Infra codes (``AgentRunError.INFRA``) are INFRA_ERROR; agent-side
    codes (schema, binding, turn or budget limit, rejected proposal) are a failed attempt (FAIL)."""
    if trial.artifact is not None:
        return
    if run is None:
        raise AgentWorkerFailed("worker_not_started")
    if run.waited_out:
        raise AgentWorkerFailed("trial_deadline")
    if run.returncode == 23 and run.error_code:
        try:
            from bravo_agents.errors import AgentRunError

            infra = run.error_code in AgentRunError.INFRA
        except ImportError:
            infra = True
        if infra:
            raise AgentWorkerFailed(f"agent_error:{run.error_code}")
        raise NoProposal(f"agent_failed:{run.error_code}")
    if run.returncode:
        raise AgentWorkerFailed(EXIT_REASONS.get(run.returncode, f"worker_exit_{run.returncode}"))
    raise NoProposal("no_valid_proposal")


class RehearsalAgent(BaseAgent):
    def __init__(self, *args: Any, trial_id: str | None = None, **kwargs: Any) -> None:
        self._trial_id = trial_id or ""
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str | None:
        try:
            return metadata.version("bravo-rehearsal")
        except metadata.PackageNotFoundError:
            return None

    async def setup(self, environment: BaseEnvironment) -> None:
        # Every agent runs only inside a Harbor sandbox: our namespace environment, agent role.
        if not isinstance(environment, RehearsalNamespaceEnvironment) or environment.is_verifier:
            raise AgentRefused("sandbox_not_harbor_namespace")

    def _binding(self) -> harbor_binding.Binding:
        binding = harbor_binding.get(self._trial_id)
        if binding is None:
            raise AgentRefused("trial_binding_missing")
        return binding

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await self.setup(environment)
        assert isinstance(environment, RehearsalNamespaceEnvironment)
        binding = self._binding()
        trial = binding.trial
        check_preconditions(trial)
        trial.box = environment.sandbox
        run: WorkerRun | None = None
        try:
            run = await run_worker(binding, self.logs_dir.parent / "platform")
        finally:
            trial.locked = True  # 07 3 "Trial 종료" 1: no tool call is accepted from here on
            self._write_trajectory(trial)
            context.metadata = {"trial_id": trial.trial_id, "artifact_digest": trial.artifact_digest,
                                "tool_calls": len(trial.tool_events),
                                "tool_calls_blocked": sum(1 for e in trial.tool_events if not e["allowed"]),
                                **({"worker": run.usage} if run is not None and run.usage else {})}
            if run is not None and isinstance(run.usage.get("total_cost_usd"), (int, float)):
                context.cost_usd = float(run.usage["total_cost_usd"])
        raise_for_outcome(trial, run)

    def _write_trajectory(self, trial: Trial) -> None:
        with contextlib.suppress(OSError):
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            doc = trajectory(trial, self.version() or "unknown").model_dump(mode="json", exclude_none=True)
            (self.logs_dir / "trajectory.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
