"""HTTP for the front switch (T17). Include with ``app.include_router(front_api.router)``.

    GET  /v1/front        current mode (cname | lb | unconfigured), active environment, tunnels, one probe
    POST /v1/front/shift  {target_env, steps?, plan_id?, epoch?} + Idempotency-Key -> 202 {run_id}
                          runs.kind = "shift"; each step is a run_stages row, each Cloudflare change an
                          effects row (resource edge/cloudflare/dns:<front> or edge/cloudflare/lb:<front>)

Needs ``request.app.state.service`` (ExecutorService: ``.ledger``, ``.observe_only``) and a
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
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .front import CloudflareError, FrontConfig, FrontSwitch, HttpProbe
from .ledger import IdempotencyConflict, RunRequest, RunStatus
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
    plan_id = body.plan_id or uuid.uuid5(uuid.NAMESPACE_URL, f"bravo:front-shift:{idempotency_key}")
    lock = _lock(request)
    tasks: set[asyncio.Task[Any]] = getattr(request.app.state, "front_tasks", set())
    request.app.state.front_tasks = tasks
    async with lock:
        req = RunRequest(plan_id=plan_id, epoch=body.epoch, idem_key=idempotency_key, kind=SHIFT)
        existing = await svc.ledger.find_run_by_key(idempotency_key)
        if existing is None and any(not t.done() for t in tasks):
            raise HTTPException(status_code=409, detail={"code": "SHIFT_IN_PROGRESS"})
        try:
            run_id, created = await svc.ledger.create_or_get_run(req)
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "run_id": str(exc.run_id)}
            ) from exc
        if created:
            task = asyncio.create_task(_run_shift(request.app, run_id, body, cfg), name=f"shift-{run_id}")
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    return ShiftAccepted(run_id=run_id)


async def _run_shift(app: Any, run_id: uuid.UUID, body: ShiftBody, cfg: FrontConfig) -> None:
    ledger = app.state.service.ledger
    rec = StageRecorder(ledger, run_id)
    fx = EffectRunner(ledger, run_id, body.epoch)
    sent = {"count": 0}

    async def apply(resource: str, action: str, call: Any, observe: Any) -> dict[str, Any]:
        sent["count"] += 1
        return await fx.apply(resource, action, call, observe)

    async def record_step(name: str, detail: dict[str, Any]) -> None:
        async with rec.stage(name) as st:
            st.detail.update(detail)

    status = RunStatus.DONE
    fs: FrontSwitch | None = None
    probe: HttpProbe | None = None
    try:
        fs = await FrontSwitch.open(cfg, _broker(app))
        probe = HttpProbe(f"https://{fs.front}{cfg.probe_path}", cfg.probe_timeout_s, cfg.probe_interval_s)
        async with rec.stage("front.preflight") as st:
            state = await fs.state()
            st.detail.update({"mode": state.mode, "active_env": state.active_env, "target_env": body.target_env,
                              "link_note": "Cloudflare API from the platform"})
        summary = await fs.shift(body.target_env, body.steps or [10, 50, 100], apply, probe, record_step)
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
        for closer in (probe, fs):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        await ledger.finish_run(run_id, status, None)
        log.info("shift run %s: %s", run_id, status)
