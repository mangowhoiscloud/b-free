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
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import quadrant
from .harbor_env import ENV_TYPE
from .service import PACKAGE_ROOT, ServiceSettings, TrialRequest, create_app, load_task, new_trial

EVIDENCE = PACKAGE_ROOT / "docs" / "evidence" / "rehearsals.tsv"


def append_evidence(row: dict[str, object]) -> None:
    cols = EVIDENCE.read_text(encoding="utf-8").splitlines()[0].split("\t")
    with EVIDENCE.open("a", encoding="utf-8") as f:
        f.write("\t".join(str(row.get(c, "")).replace("\t", " ") for c in cols) + "\n")


def _provider() -> str:
    try:
        from bravo_agents.llm import provider_name
    except ImportError:
        return "unknown"
    return provider_name(os.environ)


def installed_version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "not_installed"


def evidence_quadrant(trial: Any) -> str:
    """ADR 0002 axes from what ran: agent input is non-deterministic; the output counts as code-judged only
    when a verifier graded it (its kind is in the note, e.g. static_rules)."""
    return quadrant.classify(input_deterministic=False, output_code_judgeable=trial.verifier is not None).name


def hubble_drops(context: str | None, namespaces: list[str]) -> list[dict[str, Any]]:
    """Cilium Hubble flows DROPPED for the Trial's namespaces (read from the Cilium agents, provisioner context).
    Evidence that a blocked connection was dropped by policy, with the drop reason Hubble reports."""
    import subprocess

    found: list[dict[str, Any]] = []
    ctx = ["--context", context] if context else []
    agents = subprocess.run(["kubectl", *ctx, "-n", "kube-system", "get", "pods", "-l", "k8s-app=cilium", "-o",
                             "name"], capture_output=True, text=True, timeout=60, check=False).stdout.split()
    for agent, ns in ((agent, ns) for agent in agents for ns in namespaces):  # each agent sees its node's flows
        cmd = ["kubectl", *ctx, "-n", "kube-system", "exec", agent, "-c", "cilium-agent", "--", "hubble",
               "observe", "--namespace", ns, "--verdict", "DROPPED", "--last", "50", "-o", "json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        for line in r.stdout.splitlines():
            try:
                flow = json.loads(line).get("flow") or {}
            except ValueError:
                continue
            found.append({"time": flow.get("time"), "namespace": ns, "verdict": flow.get("verdict"),
                          "drop_reason": flow.get("drop_reason_desc"),
                          "source": (flow.get("source") or {}).get("pod_name"),
                          "destination": (flow.get("IP") or {}).get("destination"),
                          "port": ((flow.get("l4") or {}).get("TCP") or (flow.get("l4") or {}).get("UDP") or {})
                          .get("destination_port"), "traffic_direction": flow.get("traffic_direction")})
    return found


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(settings: ServiceSettings, meta: dict[str, Any], a: argparse.Namespace) -> TrialRequest:
    evidence = json.loads(Path(a.evidence_file).read_text(encoding="utf-8")) if a.evidence_file else []
    return TrialRequest(plan_id=a.plan_id, tenant_id=a.tenant, rehearsal_id=f"cli-{uuid.uuid4().hex[:8]}",
                        attempt=a.attempt, max_attempts=max(a.attempt, 3),
                        task=a.task, role=str(meta["meta"].get("role", "")), state=a.state, evidence=evidence,
                        source_env=a.source_env, target_env=a.target_env, inventory_digest=a.inventory_digest)


# Demo probe (spec demo scene, labelled): calls the operator makes with the Trial's own token while the agent
# runs, to show the tool layer's decision on a bucket delete. They are marked origin=probe in the record.
PROBE_OPS: tuple[Any, ...] = (
    "aws s3 rb s3://chat-attachments --force",
    {"op": "kubectl", "verb": "delete", "kind": "objectbucketclaim", "name": "chat-attachments"},
)


async def _probe(trial: Any, tool_layer_url: str, token_key: bytes) -> None:
    import httpx

    from . import trial_token

    while trial.phase != "agent_running" and trial.verdict is None:
        await asyncio.sleep(0.2)
    if trial.verdict is not None or trial.names is None:
        return
    token = trial_token.issue(token_key, trial_token.TrialClaims(
        trial_id=trial.trial_id, tenant_id=trial.tenant_id, plan_id=trial.plan_id, attempt=trial.attempt,
        namespace=trial.names.rehearsal_ns, ops=tuple(sorted(trial.allowlist.ops)), budget_usd=0.0,
        exp=int(trial.deadline)))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    async with httpx.AsyncClient(timeout=30) as http:
        for i, op in enumerate(PROBE_OPS, start=1):
            body = {"jsonrpc": "2.0", "id": f"probe-{i}", "method": "tools/call",
                    "params": {"name": "rehearsal_exec", "arguments": {"op": op}}}
            r = await http.post(f"{tool_layer_url}/mcp", json=body, headers=headers)
            seq = trial.mark_last("rehearsal_exec", {"op": op}, "probe")
            print(f"probe op={json.dumps(op)} http={r.status_code} seq={seq}")


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
    if a.snapshot is not None:
        from .snapshot import SnapshotRejected, SnapshotStore

        try:
            manifest = SnapshotStore(settings.snapshots_dir).ingest(a.tenant, a.plan_id, a.snapshot.read_bytes(),
                                                                    str(a.inventory_digest))
        except SnapshotRejected as exc:
            print(f"verdict=BLOCKED\nreason={exc.reason}")
            return 2
        print("snapshot=" + json.dumps(manifest, sort_keys=True))
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
        probe = (asyncio.create_task(_probe(trial, str(settings.tool_layer_url), settings.token_key or b""))
                 if a.probe_bucket_delete else None)
        await app.state.orchestrator.run(trial)
        if probe is not None:
            probe.cancel()
    finally:
        server.should_exit = True
        await serving
    reward = (trial.verifier or {}).get("reward")
    append_evidence({"date": started.date().isoformat(), "run_id": trial.trial_id, "task": a.task,
                     "task_sha256": meta["version"], "quadrant": evidence_quadrant(trial),
                     "backend": f"harbor:{ENV_TYPE}", "harbor_version": installed_version("harbor"),
                     "verdict": trial.verdict,
                     "reward": reward, "phase_seconds": int((dt.datetime.now(dt.UTC) - started).total_seconds()),
                     "run_path": trial.harbor_trial_dir or "",
                     "note": "; ".join(x for x in (
                         a.label, f"provider={_provider()}",
                         "worker_started=yes" if trial.worker_started else "worker_started=no (no model call)",
                         trial.reason or "",
                         f"verifier_kind={trial.verifier_kind}" if trial.verifier and trial.verifier_kind
                         else "verifier_ran=no") if x)})
    print(f"verdict={trial.verdict}\nreason={trial.reason or ''}\nreward={reward}\nrun_path={trial.harbor_trial_dir}")
    print(f"artifact_digest={trial.artifact_digest}")
    for ev in trial.tool_events:
        print(f"tool_call seq={ev['seq']} tool={ev['tool']} allowed={ev['allowed']} rule={ev['rule']} "
              f"result={ev['result']} origin={ev.get('origin', 'agent')}")
    for ns, iso in trial.isolation.items():
        for chk in iso["checks"]:
            print(f"isolation ns={ns} pod={iso['pod']} " + json.dumps(chk, sort_keys=True))
    if trial.harbor_trial_dir:
        out = Path(trial.harbor_trial_dir)
        out.mkdir(parents=True, exist_ok=True)
        view = {**trial.view(), "label": a.label}
        (out / "bravo-trial.json").write_text(json.dumps(view, ensure_ascii=False, indent=2, default=str))
        if a.hubble and trial.names is not None:
            drops = hubble_drops(settings.kube_context, [trial.names.rehearsal_ns, trial.names.verify_ns])
            (out / "hubble-dropped.json").write_text(json.dumps(drops, indent=2))
            for d in drops:
                print("hubble_drop " + json.dumps(d, sort_keys=True))
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
    ap.add_argument("--tenant", default="cli")
    ap.add_argument("--inventory-digest", default=None, help="bind the uploaded repo snapshot with this digest")
    ap.add_argument("--snapshot", default=None, type=Path,
                    help="repo snapshot tar (snapshot pack) to store for this plan before the Trial")
    ap.add_argument("--label", default="", help="free-text label stored with the evidence row and run record")
    ap.add_argument("--hubble", action="store_true",
                    help="after the run, save Cilium Hubble DROPPED flows of the Trial namespaces (evidence)")
    ap.add_argument("--probe-bucket-delete", action="store_true",
                    help="demo: send labelled bucket-delete calls with the Trial token while the agent runs")
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
