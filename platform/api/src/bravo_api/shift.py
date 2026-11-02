"""Console start of a move through SHIFT, failback start, and the run watcher (contract 15 §10, CONTRACT.md 6-7).

Cutover start (console operator) from AWAIT_APPROVAL, one transaction: lock plan -> no active run -> valid,
unexpired, unconsumed ``cutover`` approval bound to the current revision and gate verdict -> consume it
(``consumed_by`` = the operator, ``run_idem_key`` = ``cutover-<approval_id>``) -> SHIFT, epoch + 1. Commit once.
Then the Executor's ``POST /v1/front/shift`` with Idempotency-Key ``shift-<approval_id>``: Edge weights
10/50/100 % to the destination, each step judged on real chat writes (FR-8). The approval covers SHIFT and
CUTOVER; the Executor checks it for both (console start = consumed, SHIFT_NOT_DONE for the cutover).

Failback (S4) follows the same transaction from WATCH with a ``failback`` approval bound to the cutover run it
reverses and calls ``POST /v1/failback``; its last step moves the front back to the source.

The watcher turns run stages written by the Executor into events and settles the plan by run kind:
  shift    DONE -> CUTOVER, then the system starts the cutover with the same approval (``cutover-<approval_id>``);
           ABORT_PRE_WRITE (reverted to 0 %, S3 1a, or refused before any change) -> AWAIT_APPROVAL with a
           fresh request; SAFE_HOLD (a weight change not confirmed) -> SAFE_HOLD.
  cutover  DONE -> WATCH (failback request), ABORT_PRE_WRITE -> AWAIT_APPROVAL, SAFE_HOLD -> SAFE_HOLD.
  failback DONE -> DONE_ROLLED_BACK (``state_reason`` ``front_not_moved`` when step 8 did not move the front),
           ABORT_PRE_WRITE -> WATCH, SAFE_HOLD -> SAFE_HOLD.
A request whose answer is lost is reconciled by reading the Executor's ``runs`` row for its key, never re-sent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from opentelemetry import trace

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .db import Conn, Database
from .errors import BusinessError
from .events import SYSTEM, Actor, EventKind, EventMode, emit, receipt_body
from .executor_client import ExecutorAmbiguous, ExecutorClient, ExecutorUnreachable
from .models import ShiftRequest
from .statemachine import State, Trigger, lock_plan, transition

LOG = logging.getLogger("bravo_api.shift")
TRACER = trace.get_tracer("bravo_api.shift")
TERMINAL = {"DONE": Trigger.RUN_DONE, "ABORT_PRE_WRITE": Trigger.RUN_ABORTED_PRE_WRITE, "SAFE_HOLD": Trigger.RUN_SAFE_HOLD}
RUN_STATES = frozenset({State.SHIFT.value, State.CUTOVER.value, State.FAILBACK.value})
RECEIPT_GRACE_POLLS = 10
FRONT_BACK_STAGE = "failback.8_front_to_src"
DEFAULT_WRITE_PAUSE_BUDGET_MS = 5000


def front_view(name: str, detail: Any) -> dict[str, Any] | None:
    """What a console needs from a front stage (contract 15 §10.3-10.4); None for other stages."""
    if not isinstance(detail, dict):
        return None
    if name == FRONT_BACK_STAGE:
        return {"moved": bool(detail.get("moved")), "reason": detail.get("reason"),
                "steps": [{"weight": s.get("weight"), "judgement": s.get("judgement")}
                          for s in detail.get("steps") or [] if isinstance(s, dict)]}
    if not name.startswith("front."):
        return None
    if name.startswith("front.set_"):
        return {"weight": detail.get("weight"), "pool_weights": detail.get("pool_weights")}
    if name.startswith("front.step_"):
        return {"weight": detail.get("weight"), "applied": detail.get("applied"),
                "reason": detail.get("reason"), "judgement": detail.get("judgement")}
    if name == "front.revert_0":
        return {"at_weight": detail.get("at_weight"), "reasons": detail.get("reasons"),
                "pool_weights": detail.get("pool_weights")}
    if name == "front.preflight":
        return {k: detail.get(k) for k in ("origin", "mode", "active_env", "target_env", "pool_weights")}
    if name == "front.summary":
        return {k: detail.get(k) for k in ("mode", "target_env", "reverted", "unhealthy", "judge")}
    if name == "front.failed":
        return {"error": detail.get("error"), "changes_sent": detail.get("changes_sent")}
    return None


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


async def shift_summary(conn: Conn, plan_id: UUID) -> dict[str, Any] | None:
    """``GET /v1/plans/{id}`` ``shift`` (contract 15 §10.2): the plan's last shift run, step by step."""
    cur = await conn.execute(
        "SELECT run_id, status, started_at, ended_at FROM runs WHERE plan_id = %s AND kind = 'shift' "
        "ORDER BY started_at DESC LIMIT 1",
        (plan_id,),
    )
    run = await cur.fetchone()
    if run is None:
        return None
    cur = await conn.execute(
        "SELECT name, detail FROM run_stages WHERE run_id = %s AND name LIKE 'front.%%' ORDER BY seq",
        (run["run_id"],),
    )
    steps: list[dict[str, Any]] = []
    current: int | None = None
    target: str | None = None
    reverted = None
    for st in await cur.fetchall():
        d = st["detail"] if isinstance(st["detail"], dict) else {}
        if st["name"] == "front.preflight":
            target = d.get("target_env")
        elif st["name"].startswith("front.set_"):
            current = d.get("weight")
        elif st["name"].startswith("front.step_"):
            raw = d.get("judgement")
            j: dict[str, Any] = raw if isinstance(raw, dict) else {}
            steps.append({"weight": d.get("weight"), **j, "hold_ms": d.get("hold_ms", j.get("hold_ms")),
                          "step_ms": d.get("step_ms", j.get("step_ms"))})
        elif st["name"] == "front.revert_0":
            current = 0
            reverted = {"at_weight": d.get("at_weight"), "reasons": d.get("reasons") or []}
    return {"run_id": str(run["run_id"]), "status": run["status"], "target_env": target, "current_weight": current,
            "started_at": _iso(run["started_at"]), "ended_at": _iso(run["ended_at"]), "steps": steps,
            "reverted": reverted}


async def _consumed_cutover_approval(conn: Conn, plan_id: UUID) -> UUID | None:
    cur = await conn.execute(
        "SELECT approval_id FROM approvals WHERE plan_id = %s AND stage = 'cutover' AND consumed_at IS NOT NULL "
        "ORDER BY consumed_at DESC LIMIT 1",
        (plan_id,),
    )
    row = await cur.fetchone()
    return None if row is None else row["approval_id"]


async def _revert_reason(conn: Conn, run_id: UUID) -> str:
    cur = await conn.execute(
        "SELECT name, detail FROM run_stages WHERE run_id = %s AND name IN ('front.revert_0', 'front.failed') "
        "ORDER BY seq DESC LIMIT 1",
        (run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return "shift_reverted"
    detail = row["detail"] or {}
    if row["name"] == "front.revert_0":
        reasons = detail.get("reasons") or []
        return f"shift_reverted:{reasons[0]}" if reasons else "shift_reverted"
    err = (detail.get("error") or {}).get("error") if isinstance(detail.get("error"), dict) else None
    return f"shift_refused:{err}" if err else "shift_refused"


class RunWatcher:
    def __init__(
        self, db: Database, poll_interval_s: float, approval_ttl_s: int, executor: ExecutorClient | None = None
    ) -> None:
        self._db = db
        self._poll = poll_interval_s
        self._approval_ttl_s = approval_ttl_s
        self._executor = executor
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def watch(self, tenant_id: str, plan_id: UUID, run_id: UUID) -> None:
        if run_id in self._tasks and not self._tasks[run_id].done():
            return
        task = asyncio.create_task(self._run(tenant_id, plan_id, run_id), name=f"run-watch-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    async def resume(self) -> int:
        """Re-attach to runs that were in flight before a restart (§8.2).

        A plan in SHIFT, CUTOVER or FAILBACK without a recorded run is reconciled by reading the Executor's
        ``runs`` row for the request's Idempotency-Key, never by sending the request again. SHIFT's key is
        ``shift-<approval_id>``, derived from the consumed approval's ``cutover-<approval_id>``.
        """
        async with self._db.tenant_tx("") as conn:
            cur = await conn.execute(
                "SELECT tenant_id, plan_id, run_id, run_idem_key, state FROM bravo_inflight_runs()"
            )
            rows = await cur.fetchall()
        for r in rows:
            run_id = r["run_id"]
            if run_id is None:
                key = r["run_idem_key"]
                if r["state"] == State.SHIFT.value and key and key.startswith("cutover-"):
                    key = "shift-" + key.removeprefix("cutover-")
                run_id = await reconcile(self._db, r["tenant_id"], r["plan_id"], key, "restart")
            if run_id is not None:
                self.watch(r["tenant_id"], r["plan_id"], run_id)
        return len(rows)

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, tenant_id: str, plan_id: UUID, run_id: UUID) -> None:
        grace = 0
        while True:
            try:
                done = await self._step(tenant_id, plan_id, run_id, final=grace >= RECEIPT_GRACE_POLLS)
                if done is None:
                    return
                if done:
                    grace += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # keep watching; the run is the source of truth
                LOG.exception("run watcher step failed run_id=%s", run_id)
            await asyncio.sleep(self._poll)

    async def _step(self, tenant_id: str, plan_id: UUID, run_id: UUID, final: bool) -> bool | None:
        """One poll. Returns None when settled, True when terminal but waiting for a receipt."""
        start_cutover = False
        async with self._db.tenant_tx(tenant_id) as conn:
            cur = await conn.execute(
                "SELECT COALESCE(max((data->>'seq')::int), 0) AS seq FROM events "
                "WHERE plan_id = %s AND run_id = %s AND kind = 'run.stage'",
                (plan_id, run_id),
            )
            last = (await cur.fetchone() or {"seq": 0})["seq"]
            cur = await conn.execute(
                "SELECT seq, name, started_at, ms, result, detail FROM run_stages WHERE run_id = %s AND seq > %s "
                "ORDER BY seq",
                (run_id, last),
            )
            for st in await cur.fetchall():
                data: dict[str, Any] = {
                    "seq": st["seq"],
                    "name": st["name"],
                    "ms": st["ms"],
                    "result": st["result"],
                    "started_at": st["started_at"].isoformat(),
                }
                front = front_view(st["name"], st["detail"])
                if front is not None:
                    data["front"] = front
                await emit(conn, plan_id=plan_id, kind=EventKind.RUN_STAGE, mode=EventMode.LIVE, actor=SYSTEM,
                           run_id=run_id, data=data)
            cur = await conn.execute("SELECT status, write_pause_ms, kind FROM runs WHERE run_id = %s", (run_id,))
            run = await cur.fetchone()
            if run is None or run["status"] not in TERMINAL:
                return False
            cur = await conn.execute(
                "SELECT receipt_id, verdict, body FROM receipts WHERE run_id = %s ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            receipt = await cur.fetchone()
            shift_run = run["kind"] == "shift"
            if receipt is None and run["status"] == "DONE" and not shift_run and not final:
                return True
            plan = await lock_plan(conn, plan_id)
            if plan["state"] not in RUN_STATES or plan["active_run_id"] != run_id:
                return None  # already settled by another watcher
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.RUN_DONE,
                mode=EventMode.LIVE,
                actor=SYSTEM,
                run_id=run_id,
                data={"kind": run["kind"], "status": run["status"], "write_pause_ms": run["write_pause_ms"]},
            )
            if receipt is not None:
                await emit(
                    conn,
                    plan_id=plan_id,
                    kind=EventKind.RECEIPT_SAVED,
                    mode=EventMode.LIVE,
                    actor=SYSTEM,
                    run_id=run_id,
                    data={
                        "receipt_id": str(receipt["receipt_id"]),
                        "verdict": receipt["verdict"],
                        "body": receipt_body(receipt["body"]),
                    },
                )
            updates = {"active_run_id": None, "last_run_id": run_id}
            if shift_run and plan["state"] == State.SHIFT.value:
                if run["status"] == "DONE":
                    await transition(conn, plan_id, Trigger.SHIFT_DONE, actor=SYSTEM, run_id=run_id,
                                     updates=updates, data={"shift_run_id": str(run_id)})
                    start_cutover = True
                elif run["status"] == "ABORT_PRE_WRITE":
                    await transition(conn, plan_id, Trigger.SHIFT_REVERTED, actor=SYSTEM, run_id=run_id,
                                     reason=await _revert_reason(conn, run_id), updates=updates,
                                     approval_ttl_s=self._approval_ttl_s)
                else:
                    await transition(conn, plan_id, Trigger.RUN_SAFE_HOLD, actor=SYSTEM, run_id=run_id,
                                     reason="shift_unconfirmed", updates=updates)
            else:
                trigger = TERMINAL[run["status"]]
                reason = None if run["status"] == "DONE" else f"run_{run['status'].lower()}"
                if run["status"] == "DONE" and plan["state"] == State.FAILBACK.value:
                    cur = await conn.execute(
                        "SELECT result FROM run_stages WHERE run_id = %s AND name = %s", (run_id, FRONT_BACK_STAGE)
                    )
                    front = await cur.fetchone()
                    if front is None or front["result"] != "OK":
                        reason = "front_not_moved"  # the DB is back on the source; the front still needs a person
                await transition(
                    conn,
                    plan_id,
                    trigger,
                    actor=SYSTEM,
                    run_id=run_id,
                    reason=reason,
                    updates=updates,
                    approval_ttl_s=self._approval_ttl_s,  # entering WATCH issues the failback request
                )
                if trigger is Trigger.RUN_ABORTED_PRE_WRITE and plan["state"] == State.CUTOVER.value:
                    await transition(
                        conn,
                        plan_id,
                        Trigger.ABORT_SETTLED,
                        actor=SYSTEM,
                        run_id=run_id,
                        reason="abort_pre_write_rolled_back",
                        approval_ttl_s=self._approval_ttl_s,
                    )
        if start_cutover:
            await self.start_cutover(tenant_id, plan_id)
        return None

    async def start_cutover(self, tenant_id: str, plan_id: UUID) -> UUID | None:
        """SHIFT reached 100 % (plan now CUTOVER): start the DB cutover with the approval the console start
        consumed. Executor refusal -> ABORT_PRE_WRITE -> AWAIT_APPROVAL; lost answer -> reconcile or SAFE_HOLD."""
        async with self._db.tenant_tx(tenant_id) as conn:
            plan = await lock_plan(conn, plan_id)
            if plan["state"] != State.CUTOVER.value or plan["active_run_id"] is not None:
                return None
            approval_id = await _consumed_cutover_approval(conn, plan_id)
            epoch = plan["epoch"]
            budget = plan.get("write_pause_budget_ms") or DEFAULT_WRITE_PAUSE_BUDGET_MS
        if approval_id is None or self._executor is None:
            await self._abort_cutover(tenant_id, plan_id, "approval_missing" if approval_id is None
                                      else "executor_not_configured")
            return None
        key = f"cutover-{approval_id}"
        with TRACER.start_as_current_span("executor.cutover") as span:
            span.set_attribute("bravo.plan_id", str(plan_id))
            try:
                run_id = await self._executor.start_cutover(
                    plan_id=plan_id, epoch=epoch, approval_id=approval_id, write_pause_budget_ms=budget,
                    idempotency_key=key,
                )
            except ExecutorUnreachable as exc:
                await self._abort_cutover(tenant_id, plan_id, exc.reason)
                return None
            except ExecutorAmbiguous as exc:
                found = await reconcile(self._db, tenant_id, plan_id, key, exc.reason)
                if found is None:
                    return None
                run_id = found
        async with self._db.tenant_tx(tenant_id) as conn:
            await lock_plan(conn, plan_id)
            await conn.execute(
                "UPDATE plans SET active_run_id = %s, last_run_id = %s, updated_at = now(), version = version + 1 "
                "WHERE plan_id = %s",
                (run_id, run_id, plan_id),
            )
        self.watch(tenant_id, plan_id, run_id)
        return run_id

    async def _abort_cutover(self, tenant_id: str, plan_id: UUID, reason: str) -> None:
        async with self._db.tenant_tx(tenant_id) as conn:
            await transition(conn, plan_id, Trigger.RUN_ABORTED_PRE_WRITE, actor=SYSTEM, reason=reason)
            await transition(conn, plan_id, Trigger.ABORT_SETTLED, actor=SYSTEM, reason=reason,
                             approval_ttl_s=self._approval_ttl_s)


async def reconcile(db: Database, tenant_id: str, plan_id: UUID, idem_key: str | None, cause: str) -> UUID | None:
    """Find the run the Executor created for ``idem_key``. Attach it, or SAFE_HOLD the plan."""
    async with db.tenant_tx(tenant_id) as conn:
        plan = await lock_plan(conn, plan_id)
        if plan["state"] not in RUN_STATES:
            return None
        row = None
        if idem_key:
            cur = await conn.execute("SELECT run_id FROM runs WHERE idem_key = %s AND plan_id = %s", (idem_key, plan_id))
            row = await cur.fetchone()
        if row is None:
            kind = plan["state"].lower()
            await transition(conn, plan_id, Trigger.RUN_SAFE_HOLD, actor=SYSTEM, reason=f"{kind}_unconfirmed:{cause}")
            return None
        await conn.execute(
            "UPDATE plans SET active_run_id = %s, last_run_id = %s, updated_at = now(), version = version + 1 "
            "WHERE plan_id = %s",
            (row["run_id"], row["run_id"], plan_id),
        )
        return row["run_id"]


class ShiftService:
    def __init__(self, db: Database, executor: ExecutorClient, watcher: RunWatcher, approval_ttl_s: int) -> None:
        self._db = db
        self._executor = executor
        self._watcher = watcher
        self._approval_ttl_s = approval_ttl_s

    async def shift(
        self, p: Principal, channel: Channel, plan_id: UUID, req: ShiftRequest, key: str | None
    ) -> dict[str, Any]:
        authorize(p, Action.SHIFT, channel)
        key = idempotency.validate_key(key)
        digest = idempotency.request_digest({"action": "shift", "plan_id": str(plan_id), **req.model_dump(mode="json")})
        actor = Actor.of(p)
        failback = req.stage == "failback"
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="shift", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            expected = State.WATCH if failback else State.AWAIT_APPROVAL
            if plan["state"] != expected.value:
                raise BusinessError("state_mismatch", detail={"state": plan["state"]})
            if plan["active_run_id"] is not None:
                raise BusinessError("run_active")
            if plan["approval_stage"] != req.stage:
                raise BusinessError("approval_missing")
            cur = await conn.execute(
                """SELECT *, expires_at <= now() AS expired FROM approvals
                    WHERE approval_request_id = %s AND stage = %s FOR UPDATE""",
                (plan["approval_request_id"], req.stage),
            )
            approval = await cur.fetchone()
            if approval is None or approval["consumed_at"] is not None:
                raise BusinessError("approval_missing")
            bound_ok = (
                approval["bound_run_id"] == plan["last_run_id"]
                if failback
                else approval["gate_verdict_id"] == plan["last_gate_verdict_id"]
                and approval["artifact_digest"] == plan["approved_artifact_digest"]
            )
            if approval["expired"] or approval["plan_revision"] != plan["revision"] or not bound_ok:
                raise BusinessError("approval_stale")
            # The consumed approval is the Executor's proof of this console start (contract 15 §10.1).
            await conn.execute(
                "UPDATE approvals SET consumed_at = now(), consumed_by = %s, run_idem_key = %s WHERE approval_id = %s",
                (p.id, f"{req.stage}-{approval['approval_id']}", approval["approval_id"]),
            )
            epoch = plan["epoch"] + 1
            started = await transition(
                conn,
                plan_id,
                Trigger.FAILBACK_STARTED if failback else Trigger.SHIFT_STARTED,
                actor=actor,
                updates={"epoch": epoch} if failback else {"epoch": epoch,
                                                            "write_pause_budget_ms": req.write_pause_budget_ms},
                data={"approval_id": str(approval["approval_id"]), "epoch": epoch, "stage": req.stage,
                      "write_pause_budget_ms": req.write_pause_budget_ms},
            )
            response: dict[str, Any] = {
                "plan_id": str(plan_id),
                "stage": req.stage,
                "approval_id": str(approval["approval_id"]),
                "epoch": epoch,
                "run_id": None,
                "run_kind": "failback" if failback else "shift",
                "state": started["state"],
                "reason": None,
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="shift", key=key, response=response, status_code=202
            )

        exec_key = f"{'failback' if failback else 'shift'}-{approval['approval_id']}"
        with TRACER.start_as_current_span(f"executor.{'failback' if failback else 'shift'}") as span:
            span.set_attribute("bravo.plan_id", str(plan_id))
            try:
                if failback:
                    run_id = await self._executor.start_failback(
                        plan_id=plan_id,
                        epoch=epoch,
                        approval_id=approval["approval_id"],
                        write_pause_budget_ms=req.write_pause_budget_ms,
                        idempotency_key=exec_key,
                    )
                else:
                    run_id = await self._executor.start_shift(
                        plan_id=plan_id, epoch=epoch, approval_id=approval["approval_id"], idempotency_key=exec_key
                    )
            except ExecutorUnreachable as exc:
                # Provably not applied. Cutover: SHIFT -> AWAIT_APPROVAL (fresh request); failback: back to WATCH.
                return await self._settle_failure(p, plan_id, key, response, exc.reason, failback=failback)
            except ExecutorAmbiguous as exc:
                # Response lost: read the real state (the Executor's runs row for this key) instead of
                # repeating the call. Not found -> SAFE_HOLD; never reopen writes on a timeout (NFR-3).
                found = await reconcile(self._db, p.tenant_id, plan_id, exec_key, exc.reason)
                if found is None:
                    response = {**response, "state": State.SAFE_HOLD.value, "reason": exc.reason}
                    async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
                        await idempotency.complete(
                            conn, tenant_id=p.tenant_id, action="shift", key=key, response=response, status_code=202
                        )
                    return response
                run_id = found

        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            await lock_plan(conn, plan_id)
            await conn.execute(
                "UPDATE plans SET active_run_id = %s, last_run_id = %s, updated_at = now(), version = version + 1 "
                "WHERE plan_id = %s",
                (run_id, run_id, plan_id),
            )
            response = {**response, "run_id": str(run_id)}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="shift", key=key, response=response, status_code=202
            )
        self._watcher.watch(p.tenant_id, plan_id, run_id)
        return response

    async def _settle_failure(
        self, p: Principal, plan_id: UUID, key: str, response: dict[str, Any], reason: str, *, failback: bool
    ) -> dict[str, Any]:
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            trigger = Trigger.RUN_ABORTED_PRE_WRITE if failback else Trigger.SHIFT_REVERTED
            # Failback: the target stays primary, back to WATCH with a fresh request. Cutover: nothing moved,
            # back to AWAIT_APPROVAL with a fresh request.
            plan = await transition(conn, plan_id, trigger, actor=SYSTEM, reason=reason,
                                    approval_ttl_s=self._approval_ttl_s)
            response = {**response, "state": plan["state"], "reason": reason}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="shift", key=key, response=response, status_code=202
            )
        return response
