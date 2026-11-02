"""Cutover and failback start (console only) and the run watcher (README "실행 시작", CONTRACT.md 6-7).

Failback (S4) follows the same transaction from WATCH with a ``failback`` approval bound to
the cutover run it reverses, and calls the Executor's ``POST /v1/failback``. Its terminal
states: DONE -> DONE_ROLLED_BACK, ABORT_PRE_WRITE -> WATCH, SAFE_HOLD -> SAFE_HOLD.

One transaction: lock plan FOR UPDATE -> state AWAIT_APPROVAL -> no active run -> valid,
unexpired, unconsumed approval bound to the current revision and gate verdict -> consume
-> CUTOVER. Commit once. Then call the Executor with an Idempotency-Key derived from the
approval, so a retry can never start a second run. The watcher turns run stages written
by the Executor into events and settles the state: DONE -> WATCH, ABORT_PRE_WRITE ->
AWAIT_APPROVAL, SAFE_HOLD -> SAFE_HOLD.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from opentelemetry import trace

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .db import Database
from .errors import BusinessError
from .events import SYSTEM, Actor, EventKind, EventMode, emit, receipt_body
from .executor_client import ExecutorAmbiguous, ExecutorClient, ExecutorUnreachable
from .models import ShiftRequest
from .statemachine import State, Trigger, lock_plan, transition

LOG = logging.getLogger("bravo_api.shift")
TRACER = trace.get_tracer("bravo_api.shift")
TERMINAL = {"DONE": Trigger.RUN_DONE, "ABORT_PRE_WRITE": Trigger.RUN_ABORTED_PRE_WRITE, "SAFE_HOLD": Trigger.RUN_SAFE_HOLD}
RUN_STATES = frozenset({State.CUTOVER.value, State.FAILBACK.value})
RECEIPT_GRACE_POLLS = 10


class RunWatcher:
    def __init__(self, db: Database, poll_interval_s: float, approval_ttl_s: int) -> None:
        self._db = db
        self._poll = poll_interval_s
        self._approval_ttl_s = approval_ttl_s
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def watch(self, tenant_id: str, plan_id: UUID, run_id: UUID) -> None:
        if run_id in self._tasks and not self._tasks[run_id].done():
            return
        task = asyncio.create_task(self._run(tenant_id, plan_id, run_id), name=f"run-watch-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    async def resume(self) -> int:
        """Re-attach to runs that were in flight before a restart (§8.2).

        A plan in CUTOVER without a recorded run is reconciled by reading the Executor's
        ``runs`` row for the request's Idempotency-Key, never by sending the request again.
        """
        async with self._db.tenant_tx("") as conn:
            cur = await conn.execute("SELECT tenant_id, plan_id, run_id, run_idem_key FROM bravo_inflight_runs()")
            rows = await cur.fetchall()
        for r in rows:
            run_id = r["run_id"]
            if run_id is None:
                run_id = await reconcile(self._db, r["tenant_id"], r["plan_id"], r["run_idem_key"], "restart")
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
        async with self._db.tenant_tx(tenant_id) as conn:
            cur = await conn.execute(
                "SELECT COALESCE(max((data->>'seq')::int), 0) AS seq FROM events "
                "WHERE plan_id = %s AND run_id = %s AND kind = 'run.stage'",
                (plan_id, run_id),
            )
            last = (await cur.fetchone() or {"seq": 0})["seq"]
            cur = await conn.execute(
                "SELECT seq, name, started_at, ms, result FROM run_stages WHERE run_id = %s AND seq > %s ORDER BY seq",
                (run_id, last),
            )
            for st in await cur.fetchall():
                await emit(
                    conn,
                    plan_id=plan_id,
                    kind=EventKind.RUN_STAGE,
                    mode=EventMode.LIVE,
                    actor=SYSTEM,
                    run_id=run_id,
                    data={
                        "seq": st["seq"],
                        "name": st["name"],
                        "ms": st["ms"],
                        "result": st["result"],
                        "started_at": st["started_at"].isoformat(),
                    },
                )
            cur = await conn.execute("SELECT status, write_pause_ms FROM runs WHERE run_id = %s", (run_id,))
            run = await cur.fetchone()
            if run is None or run["status"] not in TERMINAL:
                return False
            cur = await conn.execute(
                "SELECT receipt_id, verdict, body FROM receipts WHERE run_id = %s ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            receipt = await cur.fetchone()
            if receipt is None and run["status"] == "DONE" and not final:
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
                data={"status": run["status"], "write_pause_ms": run["write_pause_ms"]},
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
            trigger = TERMINAL[run["status"]]
            reason = None if run["status"] == "DONE" else f"run_{run['status'].lower()}"
            await transition(
                conn,
                plan_id,
                trigger,
                actor=SYSTEM,
                run_id=run_id,
                reason=reason,
                updates={"active_run_id": None, "last_run_id": run_id},
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
            return None


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
            kind = "cutover" if plan["state"] == State.CUTOVER.value else "failback"
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
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="shift", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            failback = req.stage == "failback"
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
            exec_key = f"{req.stage}-{approval['approval_id']}"
            await conn.execute(
                "UPDATE approvals SET consumed_at = now(), consumed_by = %s, run_idem_key = %s WHERE approval_id = %s",
                (p.id, exec_key, approval["approval_id"]),
            )
            epoch = plan["epoch"] + 1
            started = await transition(
                conn,
                plan_id,
                Trigger.FAILBACK_STARTED if failback else Trigger.CUTOVER_STARTED,
                actor=actor,
                updates={"epoch": epoch},
                data={"approval_id": str(approval["approval_id"]), "epoch": epoch, "stage": req.stage},
            )
            response: dict[str, Any] = {
                "plan_id": str(plan_id),
                "stage": req.stage,
                "approval_id": str(approval["approval_id"]),
                "epoch": epoch,
                "run_id": None,
                "state": started["state"],
                "reason": None,
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="shift", key=key, response=response, status_code=202
            )

        start = self._executor.start_failback if failback else self._executor.start_cutover
        with TRACER.start_as_current_span(f"executor.{req.stage}") as span:
            span.set_attribute("bravo.plan_id", str(plan_id))
            try:
                run_id = await start(
                    plan_id=plan_id,
                    epoch=epoch,
                    approval_id=approval["approval_id"],
                    write_pause_budget_ms=req.write_pause_budget_ms,
                    idempotency_key=exec_key,
                )
            except ExecutorUnreachable as exc:
                # Provably not applied: settle as ABORT_PRE_WRITE -> AWAIT_APPROVAL. A new approval is required.
                response = await self._settle_failure(p, plan_id, key, response, exc.reason, abort=True, failback=failback)
                return response
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
        self,
        p: Principal,
        plan_id: UUID,
        key: str,
        response: dict[str, Any],
        reason: str,
        *,
        abort: bool,
        failback: bool = False,
    ) -> dict[str, Any]:
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            if abort and failback:
                # Provably not applied: the target stays primary; back to WATCH with a fresh request.
                plan = await transition(
                    conn,
                    plan_id,
                    Trigger.RUN_ABORTED_PRE_WRITE,
                    actor=SYSTEM,
                    reason=reason,
                    approval_ttl_s=self._approval_ttl_s,
                )
            elif abort:
                await transition(conn, plan_id, Trigger.RUN_ABORTED_PRE_WRITE, actor=SYSTEM, reason=reason)
                plan = await transition(
                    conn, plan_id, Trigger.ABORT_SETTLED, actor=SYSTEM, reason=reason, approval_ttl_s=self._approval_ttl_s
                )
            else:
                plan = await transition(conn, plan_id, Trigger.RUN_SAFE_HOLD, actor=SYSTEM, reason=reason)
            response = {**response, "state": plan["state"], "reason": reason}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="shift", key=key, response=response, status_code=202
            )
        return response
