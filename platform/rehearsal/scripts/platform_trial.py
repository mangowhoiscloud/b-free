"""Drive the platform rehearsal service the way the platform API does, from inside the API Pod (S2, B24/P12).

Why: the console path (console -> API ``POST /v1/plans/{id}/rehearsals``) needs a logged-in operator plan. Until the
operator has logged in, this script runs the same upstream calls with the API's own code, identity and network path:

    1. collector ``GET /v1/inventory`` and ``GET /v1/inventory/snapshot``   (``CollectorClient``, same as the API)
    2. gate ``POST /v1/controls/report`` for the plan's environments        (``GateControlsClient``, same as the API)
    3. rehearsal ``PUT /v1/snapshots/{tenant}/{plan}``                      (``RehearsalClient.put_snapshot``)
    4. rehearsal ``POST /v1/trials`` per task, body from ``trial_request``  (``RehearsalClient.start``)
    5. optional demo probes ``POST /v1/trials/{id}/probes`` (labelled ``origin=probe`` by the service)
    6. ``GET /v1/trials/{id}`` every second until a verdict                (what ``RehearsalWatcher`` polls)
    7. on FAIL, the next attempt with ``prior_failures`` (FR-5, up to 3)   (what ``settle`` + a new start do)

What the console replaces: the plan row, the state machine transition and the ``rehearsals`` row that
``RehearsalService.start`` writes in the platform DB (steps before 1), and the SSE events the watcher emits from step 6.
Steps 1-6 are the same calls. No approval, no change row, no apply: this script never touches the platform DB.

Run (stdin, nothing is copied into the image; the API token stays in the Pod's environment):

    kubectl -n bravo-control exec -i deploy/api -c api -- python3 - --tasks governance,app_migration \
        --source source-aws --target target-onprem < platform/rehearsal/scripts/platform_trial.py

Output: one JSON line per event (``progress``) and a final ``result`` line with every Trial view.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from bravo_api.config import Settings
from bravo_api.gate import judge_control
from bravo_api.rehearsal import evidence_from, prior_failures, trial_request
from bravo_api.upstreams import CollectorClient, GateControlsClient, RehearsalClient, UpstreamError

ROLE_OF = {"governance": "governance", "app_migration": "app_migration", "app-migration": "app_migration"}


def out(kind: str, **kw: Any) -> None:
    print(json.dumps({"kind": kind, "at": datetime.now(UTC).isoformat(), **kw}, ensure_ascii=False, default=str),
          flush=True)


def failure_row(view: dict[str, Any]) -> dict[str, Any]:
    """A FAIL as the API keeps it (``rehearsals`` row: attempt, reason, verifier). The API's ``prior_failures`` (this
    commit) adds the failed checks to the summary; an older API image in the Pod does not, so they go in the reason."""
    checks = ((view.get("verifier") or {}).get("checks") or [])
    failed = "; ".join(f"{c.get('name')}: {c.get('detail')}" for c in checks if isinstance(c, dict) and not c.get("ok"))
    reason = str(view.get("reason") or "")
    if failed and "_failed_checks" not in prior_failures.__code__.co_names:  # the Pod's API predates this commit
        reason = f"{reason}; failed checks: {failed}"
    return {"attempt": view.get("attempt"), "reason": reason, "verifier": view.get("verifier")}


async def gate_verdict(gate: GateControlsClient, source: str, target: str) -> dict[str, Any]:
    rows, expires = await gate.report(source, target)
    controls = []
    for r in rows:
        verdict, reason = judge_control(str(r.get("src")), str(r.get("dst")))
        controls.append({"control_id": r["control_id"], "src": r.get("src"), "dst": r.get("dst"),
                         "verdict": verdict, "reason": reason})
    # The API's verdict id is its gate_verdicts row; here a fresh id names this report in the evidence ids.
    return {"verdict_id": str(uuid.uuid4()), "controls": controls, "expires_at": expires}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True, help="comma list: governance,app_migration")
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--tenant", default="b-free")
    ap.add_argument("--plan-id", default=None, help="one plan for every task (default: new uuid)")
    ap.add_argument("--rehearsal-url", default=None, help="override BRAVO_REHEARSAL_URL (e.g. a preview Pod IP)")
    ap.add_argument("--probe", action="store_true", help="send the demo probes once each Trial runs (origin=probe)")
    ap.add_argument("--skip-gate", action="store_true", help="no gate report (no gate rows in the evidence)")
    ap.add_argument("--max-s", type=int, default=1800)
    ap.add_argument("--prior-trials", default="", help="resume after earlier FAILs of ONE task: comma list of their Trial ids")
    ap.add_argument("--prior-url", default=None, help="rehearsal service that holds those Trials (default: --rehearsal-url)")
    ap.add_argument("--drops-wait-s", type=float, default=90.0, help="wait for the service's Hubble drop counters")
    a = ap.parse_args()

    s = Settings.from_env()
    token = os.environ.get("BRAVO_REHEARSAL_API_TOKEN")
    rurl = a.rehearsal_url or s.rehearsal_url
    if not (s.collector_url and rurl and token):
        out("blocked", reason="collector_or_rehearsal_unconfigured")
        return 2
    collector = CollectorClient(s.collector_url, max(s.rehearsal_timeout_s, 30.0))
    rehearsal = RehearsalClient(rurl, max(s.rehearsal_timeout_s, 30.0), token)
    plan_id = a.plan_id or str(uuid.uuid4())
    t0 = time.monotonic()
    try:
        inventory = await collector.inventory()
        digest = inventory["input_digest"]
        snapshot = await collector.snapshot(digest)
        out("inventory", plan_id=plan_id, input_digest=digest, files=inventory.get("files_scanned"),
            findings=len(inventory.get("findings") or []), snapshot_bytes=len(snapshot), s=round(time.monotonic() - t0, 1))
        gate = None
        if not a.skip_gate and s.gate_controls_url:
            gate = await gate_verdict(GateControlsClient(s.gate_controls_url, max(s.gate_timeout_s, 150.0)),
                                      a.source, a.target)
            out("gate", verdict_id=gate["verdict_id"],
                blocked=[c["control_id"] for c in gate["controls"] if c["verdict"] not in ("PASS", "EXCEPTION")],
                s=round(time.monotonic() - t0, 1))
        manifest = await rehearsal.put_snapshot(a.tenant, plan_id, snapshot, digest)
        out("snapshot", manifest=manifest, s=round(time.monotonic() - t0, 1))
        evidence = evidence_from(inventory, gate)
    except UpstreamError as exc:
        out("blocked", reason=exc.reason, status=exc.status, body=exc.body)
        return 2

    t_start = time.monotonic()
    tasks = [t.strip() for t in a.tasks.split(",") if t.strip()]
    failures: dict[str, list[dict[str, Any]]] = {t: [] for t in tasks}
    prior_src = RehearsalClient(a.prior_url, 30.0, token) if a.prior_url else rehearsal
    for tid in [x.strip() for x in a.prior_trials.split(",") if x.strip()]:  # e.g. the driver Pod was replaced
        v = await prior_src.get(tid)
        for t in tasks:
            failures[t].append(failure_row(v))
    views: dict[str, dict[str, Any]] = {}
    started: dict[str, dict[str, Any]] = {}

    async def start(task: str, attempt: int) -> None:
        rid = uuid.uuid4()
        payload = trial_request(plan_id=uuid.UUID(plan_id), tenant_id=a.tenant, rehearsal_id=rid, attempt=attempt,
                                max_attempts=s.rehearsal_max_attempts, task=task.replace("_", "-"), role=ROLE_OF[task],
                                state="PROPOSE", prior=prior_failures(failures[task]), evidence=evidence,
                                source_env=a.source, target_env=a.target, analysis=None, inventory_digest=digest)
        body = await rehearsal.start(payload, f"rehearsal-{rid}")
        started[body["trial_id"]] = {"task": task, "attempt": attempt, "rehearsal_id": str(rid), "probed": False,
                                     "phase": None, "t0": time.monotonic()}
        out("started", task=task, attempt=attempt, trial_id=body["trial_id"], phase=body.get("phase"),
            s=round(time.monotonic() - t_start, 1))

    try:
        for task in tasks:
            await start(task, len(failures[task]) + 1)
    except UpstreamError as exc:
        out("blocked", reason=exc.reason, status=exc.status, body=exc.body)
        return 2

    while time.monotonic() - t_start < a.max_s:
        for tid, st in list(started.items()):
            if tid in views and views[tid].get("verdict"):
                continue
            try:
                v = await rehearsal.get(tid)
            except UpstreamError as exc:
                out("poll_error", trial_id=tid, reason=exc.reason)
                continue
            views[tid] = v
            if v.get("phase") != st["phase"]:
                st["phase"] = v.get("phase")
                out("progress", task=st["task"], attempt=st["attempt"], trial_id=tid, phase=v.get("phase"),
                    s=round(time.monotonic() - t_start, 1))
            if a.probe and st["attempt"] == 1 and not st["probed"] and v.get("phase") == "agent_running":
                st["probed"] = True
                try:
                    r = await rehearsal._client.request("POST", f"/v1/trials/{tid}/probes", json={})  # noqa: SLF001
                    out("probe", trial_id=tid, http=r.status_code, body=r.json() if r.content else None)
                except Exception as exc:  # noqa: BLE001 - recorded, the Trial goes on
                    out("probe", trial_id=tid, error=type(exc).__name__)
            if v.get("verdict"):
                out("verdict", task=st["task"], attempt=st["attempt"], trial_id=tid, verdict=v.get("verdict"),
                    reason=v.get("reason"), s=round(time.monotonic() - t_start, 1),
                    trial_s=round(time.monotonic() - st["t0"], 1))
                if v["verdict"] == "FAIL" and st["attempt"] < s.rehearsal_max_attempts:
                    failures[st["task"]].append(failure_row(v))
                    try:
                        await start(st["task"], st["attempt"] + 1)
                    except UpstreamError as exc:
                        out("blocked", reason=exc.reason, status=exc.status)
        if started and all((views.get(t) or {}).get("verdict") for t in started):
            break
        await asyncio.sleep(1.0)
    # The service collects Hubble drop counters after the verdict (SCRAPE_WAIT_S); read the views once more.
    await asyncio.sleep(a.drops_wait_s)
    for tid in started:
        try:
            views[tid] = await rehearsal.get(tid)
        except UpstreamError:
            pass
    for tid, v in views.items():
        out("result", task=started[tid]["task"], attempt=started[tid]["attempt"], trial_id=tid,
            verdict=v.get("verdict"), reason=v.get("reason"), s=round(time.monotonic() - t_start, 1), view=v)
    last = {started[t]["task"]: views.get(t, {}).get("verdict") for t in started}  # the last attempt per task wins
    return 0 if all(v == "PASS" for v in last.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
