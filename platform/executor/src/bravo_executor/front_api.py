"""HTTP for the front switch (T17, contract 15 §10). Include with ``app.include_router(front_api.router)``.

    GET  /v1/front        current mode (cname | lb | unconfigured), active environment, tunnels, one probe
    POST /v1/front/shift  {target_env, steps?, plan_id?, epoch?, approval_id?, stage?} + Idempotency-Key
                          -> 202 {run_id}
                          runs.kind = "shift"; each step is two run_stages rows (front.set_<w> when the weight
                          is confirmed, front.step_<w> with the judgement), each Cloudflare change an effects row
                          (resource edge/cloudflare/dns:<front> or edge/cloudflare/lb:<front>)

Plan shifts (``plan_id`` given) are the SHIFT state of a plan: ``approval_id`` and ``stage`` are required and
the approval is checked like a cutover's (approvals.py: console start, separation of duties, one shift run per
approval). Without ``plan_id`` the run is a tool run (T17 evidence, deploy/cloud/front_shift.sh): no approval,
same judgement and revert.

Needs ``request.app.state.service`` (ExecutorService: ``.ledger``, ``.observe_only``, ``.approvals``) and a
``vault.SecretBroker``: ``request.app.state.secret_broker`` when the app made one at startup, else one is
created from the environment on first use (``executor-bao.sh with-executor`` sets it; the secret_id is
single-use, so create the broker once per process). Config: ``FrontConfig.from_env`` (BRAVO_FRONT_*).

Run status: DONE (all planned steps done), ABORT_PRE_WRITE (judged and reverted to 0 %, or refused before
any change, e.g. target tunnel without connections), SAFE_HOLD (a change was sent and not confirmed).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .approvals import ApprovalCheck, ApprovalRejected
from .front import CloudflareError, FrontConfig, FrontSwitch, HttpProbe, WriteSampler
from .ledger import IdempotencyConflict, RunRequest, RunStatus
from .loadmetrics import LoadgenMetrics
from .stages import EffectRunner, StageRecorder, error_evidence
from .vault import SecretBroker

log = logging.getLogger(__name__)
router = APIRouter()
SHIFT = "shift"


class ShiftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_env: str = Field(min_length=1, max_length=32)
    steps: list[int] | None = None
    plan_id: uuid.UUID | None = None
    epoch: int = Field(default=0, ge=0)
    approval_id: uuid.UUID | None = None
    stage: Literal["cutover", "failback"] | None = None


class ShiftAccepted(BaseModel):
    run_id: uuid.UUID


def _broker(app: Any) -> SecretBroker:
    broker = getattr(app.state, "secret_broker", None)
    if broker is None:
        broker = SecretBroker.from_env()
        app.state.secret_broker = broker
    return broker


def _lock(request: Request) -> asyncio.Lock:
    lock = getattr(request.app.state, "front_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.front_lock = lock
    return lock


@router.get("/v1/front")
async def get_front(request: Request) -> dict[str, Any]:
    cfg = FrontConfig.from_env()
    fs = await FrontSwitch.open(cfg, _broker(request.app))
    try:
        st = await fs.state()
        probe = HttpProbe(f"https://{fs.front}{cfg.probe_path}", cfg.probe_timeout_s, cfg.probe_interval_s)
        try:
            s = await probe.once(time.monotonic())
        finally:
            await probe.close()
        return {
            "front": st.front,
            "mode": st.mode,
            "active_env": st.active_env,
            "detail": st.detail,
            "probe": {"status": s.status, "env_id": s.env_id, "ok": s.ok, "latency_ms": round(s.latency_ms, 1)},
        }
    except CloudflareError as exc:
        raise HTTPException(status_code=502, detail={"code": "CLOUDFLARE", "message": str(exc)}) from exc
    finally:
        await fs.close()


@router.post("/v1/front/shift", status_code=202, response_model=ShiftAccepted)
async def post_shift(
    body: ShiftBody,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ShiftAccepted:
    if idempotency_key is None or not (1 <= len(idempotency_key) <= 200):
        raise HTTPException(status_code=400, detail={"code": "IDEMPOTENCY_KEY_REQUIRED"})
    svc = request.app.state.service
    if getattr(svc, "observe_only", False):
        raise HTTPException(status_code=503, detail={"code": "OBSERVE_ONLY"})
    cfg = FrontConfig.from_env()
    if body.target_env not in cfg.env_ids:
        raise HTTPException(status_code=422, detail={"code": "UNKNOWN_ENV", "known": sorted(cfg.env_ids)})
    if body.plan_id is not None and (body.approval_id is None or body.stage is None):
        # A plan's SHIFT is never a tool run: it carries the cutover approval the console start consumed.
        raise HTTPException(status_code=422, detail={"code": "APPROVAL_REQUIRED"})
    plan_id = body.plan_id or uuid.uuid5(uuid.NAMESPACE_URL, f"bravo:front-shift:{idempotency_key}")
    lock = _lock(request)
    tasks: set[asyncio.Task[Any]] = getattr(request.app.state, "front_tasks", set())
    request.app.state.front_tasks = tasks
    async with lock:
        req = RunRequest(plan_id=plan_id, epoch=body.epoch, idem_key=idempotency_key, kind=SHIFT)
        existing = await svc.ledger.find_run_by_key(idempotency_key)
        if existing is None and any(not t.done() for t in tasks):
            raise HTTPException(status_code=409, detail={"code": "SHIFT_IN_PROGRESS"})
        if existing is None:
            running = await svc.ledger.running_run_ids()
            if running:
                raise HTTPException(status_code=409, detail={"code": "RUN_IN_PROGRESS", "run_id": str(running[0])})
        approval: dict[str, Any] | None = None
        if body.approval_id is not None and body.stage is not None:
            try:
                approval = await svc.approvals.verify(ApprovalCheck(
                    approval_id=str(body.approval_id), plan_id=plan_id, kind=body.stage,
                    idem_key=idempotency_key, run_kind=SHIFT))
            except ApprovalRejected as exc:
                raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
        try:
            run_id, created = await svc.ledger.create_or_get_run(req)
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "run_id": str(exc.run_id)}
            ) from exc
        if created:
            task = asyncio.create_task(_run_shift(request.app, run_id, body, cfg, approval), name=f"shift-{run_id}")
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    return ShiftAccepted(run_id=run_id)


async def _run_shift(
    app: Any, run_id: uuid.UUID, body: ShiftBody, cfg: FrontConfig, approval: dict[str, Any] | None
) -> None:
    ledger = app.state.service.ledger
    rec = StageRecorder(ledger, run_id)
    fx = EffectRunner(ledger, run_id, body.epoch)
    sent = {"count": 0}

    async def apply(resource: str, action: str, call: Any, observe: Any) -> dict[str, Any]:
        sent["count"] += 1
        return await fx.apply(resource, action, call, observe)

    status = RunStatus.DONE
    fs: FrontSwitch | None = None
    probe: HttpProbe | None = None
    sampler: WriteSampler | None = None
    loadgen = LoadgenMetrics.from_env(None if body.plan_id is None else str(body.plan_id))
    try:
        fs = await FrontSwitch.open(cfg, _broker(app))
        probe = HttpProbe(f"https://{fs.front}{cfg.probe_path}", cfg.probe_timeout_s, cfg.probe_interval_s)
        sampler = WriteSampler(fs.front, cfg, str(run_id)[:8])
        async with rec.stage("front.preflight") as st:
            # seq 1 carries the approval: approvals.py finds "one shift run per approval" and SHIFT_NOT_DONE here.
            st.detail.update({"approval_id": None if body.approval_id is None else str(body.approval_id),
                              "approval": approval, "origin": "plan" if body.plan_id else "tool",
                              "stage": body.stage})
            state = await fs.state()
            st.detail.update({"mode": state.mode, "active_env": state.active_env, "target_env": body.target_env,
                              "pool_weights": state.detail.get("pool_weights"), "thresholds": cfg.thresholds(),
                              "link_note": "Cloudflare API from the platform"})
        summary = await fs.shift(body.target_env, body.steps or [10, 50, 100], apply, probe,
                                 _recorder(rec), sampler, loadgen=loadgen)
        if "reverted" in summary:
            status = RunStatus.ABORT_PRE_WRITE
        async with rec.stage("front.summary") as st:
            st.detail.update(summary)
    except BaseException as exc:  # noqa: BLE001 - every failure ends with a recorded status
        status = RunStatus.SAFE_HOLD if sent["count"] else RunStatus.ABORT_PRE_WRITE
        try:
            async with rec.stage("front.failed") as st:
                st.detail.update({"error": error_evidence(exc), "changes_sent": sent["count"]})
        except Exception:  # noqa: BLE001
            log.exception("shift %s: could not record failure stage", run_id)
    finally:
        for closer in (sampler, probe, fs, loadgen):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        await ledger.finish_run(run_id, status, None)
        log.info("shift run %s: %s", run_id, status)


def _recorder(rec: StageRecorder) -> Any:
    async def record_step(name: str, detail: dict[str, Any], result: str = "OK") -> None:
        # An LB step is written after its change and hold: its own step_ms is the stage time.
        if isinstance(detail.get("step_ms"), int):
            await rec.record_done(name, detail["step_ms"], detail, result)
            return
        async with rec.stage(name) as st:
            st.detail.update(detail)

    return record_step


async def move_front(app: Any, target_env: str, fx: EffectRunner, rec: StageRecorder) -> dict[str, Any]:
    """Failback front move (contract 15 §10.7): all traffic to ``target_env`` in one step, judged and recorded,
    never reverted. Raises when the move itself cannot be made or confirmed."""
    cfg = FrontConfig.from_env()
    fs = await FrontSwitch.open(cfg, _broker(app))
    probe = HttpProbe(f"https://{fs.front}{cfg.probe_path}", cfg.probe_timeout_s, cfg.probe_interval_s)
    sampler = WriteSampler(fs.front, cfg, f"failback-{target_env}")
    try:
        summary = await fs.shift(target_env, [100], fx.apply, probe, _recorder(rec), sampler, revert=False)
        return {"moved": True, **summary}
    finally:
        for closer in (sampler, probe, fs):
            try:
                await closer.close()
            except Exception:  # noqa: BLE001, S110
                pass
