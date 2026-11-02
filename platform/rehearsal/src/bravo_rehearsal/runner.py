"""One rehearsal from the command line (``make rehearse``): the same Harbor Trial as the service.

Starts the rehearsal service in-process (the agent worker needs its ``/mcp`` tool layer at
``BRAVO_REHEARSAL_TOOL_LAYER_URL``), checks the same preconditions, runs one Harbor Trial
through ``Orchestrator.run`` and appends one row to ``docs/evidence/rehearsals.tsv``. A
precondition that is not met is printed as the reason and nothing is created (exit 2).
The model provider is chosen only through the environment (``BRAVO_LLM_PROVIDER`` and its
route variables, validated by ``bravo_agents``).

    python -m bravo_rehearsal.runner --task governance [--state REHEARSE] [--source-env ID --target-env ID]

Every run is a Harbor Trial with a real sandbox: there is no mode that runs the agent without one.
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
    if missing:
        print(f"verdict=BLOCKED\nreason={missing[0]}\nmissing={','.join(missing)}")
        return 2
    url = urlparse(str(settings.tool_layer_url))
    server = uvicorn.Server(uvicorn.Config(app, host=url.hostname or "127.0.0.1", port=url.port or 80,
                                           log_level="warning", lifespan="on"))
    serving = asyncio.create_task(server.serve())
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
        await app.state.orchestrator.run(trial)
    finally:
        server.should_exit = True
        await serving
    reward = (trial.verifier or {}).get("reward")
    append_evidence({"date": started.date().isoformat(), "run_id": trial.trial_id, "task": a.task,
                     "task_sha256": meta["version"], "quadrant": "FIX",
                     "backend": "harbor:bravo-k8s-namespace", "harbor_version": "0.25.0", "verdict": trial.verdict,
                     "reward": reward, "phase_seconds": int((dt.datetime.now(dt.UTC) - started).total_seconds()),
                     "run_path": trial.harbor_trial_dir or "", "note": trial.reason or ""})
    print(f"verdict={trial.verdict}\nreason={trial.reason or ''}\nreward={reward}\nrun_path={trial.harbor_trial_dir}")
    print(f"artifact_digest={trial.artifact_digest}")
    for ev in trial.tool_events:
        print(f"tool_call seq={ev['seq']} tool={ev['tool']} allowed={ev['allowed']} rule={ev['rule']} "
              f"result={ev['result']}")
    print(f"hook_events={len(trial.hook_events)}")
    return 0 if trial.verdict == "PASS" else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="task name (folder under the task search path)")
    ap.add_argument("--plan-id", default=f"cli-{uuid.uuid4().hex[:8]}")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--state", default="REHEARSE", help="state machine state (must be allowed for the role)")
    ap.add_argument("--source-env", default=None)
    ap.add_argument("--target-env", default=None)
    ap.add_argument("--evidence-file", default=None, help="JSON list of {id, kind, summary} for the envelope")
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
