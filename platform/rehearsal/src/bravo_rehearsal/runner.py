"""One rehearsal from the command line (``make rehearse``): the same Harbor Trial as the service.

Starts the rehearsal service in-process (the agent worker needs its ``/mcp`` tool layer at
``BRAVO_REHEARSAL_TOOL_LAYER_URL``), checks the same preconditions, runs one Harbor Trial
through ``Orchestrator.run`` and appends one row to ``docs/evidence/rehearsals.tsv``. A
precondition that is not met is printed as the reason and nothing is created (exit 2).
The model provider is chosen only through the environment (``BRAVO_LLM_PROVIDER`` and its
route variables, validated by ``bravo_agents``).

    python -m bravo_rehearsal.runner --task governance [--state REHEARSE] [--source-env ID --target-env ID]

``--without-cluster`` is a diagnostic for when no destination cluster exists. It runs the same
worker against the same tool layer but there is no Harbor Trial, no sandbox and no verifier:
``rehearsal_exec`` is refused with ``sandbox_unavailable`` and the verdict is always
``INFRA_ERROR``. It shows the model route and the server-side tool-call log; it never produces
a PASS and it is not reachable from the service API.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .service import PACKAGE_ROOT, ServiceSettings, TrialRequest, create_app, load_task, new_trial

EVIDENCE = PACKAGE_ROOT / "docs" / "evidence" / "rehearsals.tsv"


def append_evidence(row: dict[str, object]) -> None:
    cols = EVIDENCE.read_text(encoding="utf-8").splitlines()[0].split("\t")
    with EVIDENCE.open("a", encoding="utf-8") as f:
        f.write("\t".join(str(row.get(c, "")).replace("\t", " ") for c in cols) + "\n")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(settings: ServiceSettings, meta: dict[str, Any], a: argparse.Namespace) -> TrialRequest:
    evidence = json.loads(Path(a.evidence_file).read_text(encoding="utf-8")) if a.evidence_file else []
    return TrialRequest(plan_id=a.plan_id, tenant_id="cli", rehearsal_id=f"cli-{uuid.uuid4().hex[:8]}",
                        attempt=a.attempt, max_attempts=max(a.attempt, 3),
                        task=a.task, role=str(meta["meta"].get("role", "")), state=a.state, evidence=evidence,
                        source_env=a.source_env, target_env=a.target_env)


async def rehearse(a: argparse.Namespace) -> int:
    import uvicorn  # service extra

    # The CLI hosts the tool layer in this process, so a per-run Trial token key and a loopback URL are
    # safe defaults (the key never leaves the process). The service itself requires both explicitly.
    os.environ.setdefault("BRAVO_REHEARSAL_TOKEN_KEY", secrets.token_hex(32))
    os.environ.setdefault("BRAVO_REHEARSAL_TOOL_LAYER_URL", f"http://127.0.0.1:{_free_port()}")
    settings = ServiceSettings.from_env()
    meta = load_task(settings.tasks_dir, a.task)
    if meta is None:
        print("verdict=BLOCKED\nreason=task_unknown")
        return 2
    req = _request(settings, meta, a)
    app = create_app(settings)
    missing = app.state.orchestrator.preconditions(req)
    if a.without_cluster:
        missing = [m for m in missing if m not in ("cluster_unconfigured", "verifier_unconfigured")]
    if missing:
        print(f"verdict=BLOCKED\nreason={missing[0]}\nmissing={','.join(missing)}")
        return 2
    url = urlparse(str(settings.tool_layer_url))
    server = uvicorn.Server(uvicorn.Config(app, host=url.hostname or "127.0.0.1", port=url.port or 80,
                                           log_level="warning", lifespan="on"))
    serving = asyncio.create_task(server.serve())
    usage: dict[str, Any] = {}
    try:
        while not server.started:
            if serving.done():
                print("verdict=BLOCKED\nreason=tool_layer_unavailable")
                return 2
            await asyncio.sleep(0.05)
        digest = hashlib.sha256(json.dumps(req.model_dump(), sort_keys=True).encode()).hexdigest()
        trial = new_trial(req, settings, f"cli-{uuid.uuid4().hex}", digest)
        app.state.store.add(trial)
        started = dt.datetime.now(dt.UTC)
        if a.without_cluster:
            usage = await _diagnostic(app.state.orchestrator, trial, meta, settings.trials_dir)
        else:
            await app.state.orchestrator.run(trial)
    finally:
        server.should_exit = True
        await serving
    reward = (trial.verifier or {}).get("reward")
    append_evidence({"date": started.date().isoformat(), "run_id": trial.trial_id, "task": a.task,
                     "task_sha256": meta["version"], "quadrant": "FIX",
                     "backend": "diagnostic:no-sandbox" if a.without_cluster else "harbor:bravo-k8s-namespace",
                     "harbor_version": "-" if a.without_cluster else "0.25.0", "verdict": trial.verdict,
                     "reward": reward, "phase_seconds": int((dt.datetime.now(dt.UTC) - started).total_seconds()),
                     "run_path": trial.harbor_trial_dir or "", "note": trial.reason or ""})
    print(f"verdict={trial.verdict}\nreason={trial.reason or ''}\nreward={reward}\nrun_path={trial.harbor_trial_dir}")
    print(f"artifact_digest={trial.artifact_digest}")
    for ev in trial.tool_events:
        print(f"tool_call seq={ev['seq']} tool={ev['tool']} allowed={ev['allowed']} rule={ev['rule']} "
              f"result={ev['result']}")
    print(f"hook_events={len(trial.hook_events)}")
    if usage:
        print("usage=" + json.dumps(usage, sort_keys=True))
    return 0 if trial.verdict == "PASS" else 1


async def _diagnostic(orch: Any, trial: Any, meta: dict[str, Any], trials_dir: Path) -> dict[str, Any]:
    """Worker + tool layer without a cluster. No sandbox and no verifier, so never a verdict but INFRA_ERROR."""
    from . import harbor_binding
    from .cluster import Names
    from .harbor_agent import AgentFailure, check_preconditions, raise_for_outcome, run_worker, trajectory

    trial.names = Names.of(trial.plan_id, trial.attempt, trial.trial_id)  # token claims; no namespace is created
    out_dir = trials_dir.resolve() / f"{trial.trial_id}.diagnostic"
    trial.harbor_trial_dir = str(out_dir)
    binding = harbor_binding.Binding(trial=trial, worker_env=lambda d: orch._worker_env(trial, meta, d))
    run = None
    try:
        check_preconditions(trial)
        run = await run_worker(binding, out_dir / "platform")
        raise_for_outcome(trial, run)
        trial.finish("INFRA_ERROR", "verifier_unavailable:no_cluster")  # a frozen proposal, but nothing graded it
    except AgentFailure as exc:
        trial.finish("INFRA_ERROR", f"{exc.reason}:no_cluster")
    finally:
        trial.locked = True
        out_dir.mkdir(parents=True, exist_ok=True)
        doc = trajectory(trial, "diagnostic").model_dump(mode="json", exclude_none=True)
        (out_dir / "trajectory.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        (out_dir / "trial.json").write_text(json.dumps(trial.view(), ensure_ascii=False, indent=2, default=str))
    return run.usage if run is not None else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="task name (folder under the task search path)")
    ap.add_argument("--plan-id", default=f"cli-{uuid.uuid4().hex[:8]}")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--state", default="REHEARSE", help="state machine state (must be allowed for the role)")
    ap.add_argument("--source-env", default=None)
    ap.add_argument("--target-env", default=None)
    ap.add_argument("--evidence-file", default=None, help="JSON list of {id, kind, summary} for the envelope")
    ap.add_argument("--without-cluster", action="store_true", help="diagnostic: no sandbox, verdict INFRA_ERROR")
    a = ap.parse_args()
    a.task = Path(a.task).name
    sys.exit(asyncio.run(_with_signals(a)))


async def _with_signals(a: argparse.Namespace) -> int:
    """SIGTERM/SIGINT cancel the run, so the worker process is terminated and the tool layer shuts down."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)  # type: ignore[union-attr]
    try:
        return await rehearse(a)
    except asyncio.CancelledError:
        print("verdict=INFRA_ERROR\nreason=runner_interrupted")
        return 130


if __name__ == "__main__":
    main()
