"""Rehearsal start and result acceptance (FR-4, FR-5, S2, §8.3-8.4, 05 3, 07 3).

The API never runs an agent and never judges a proposal. It asks platform/rehearsal for one
Trial per attempt and accepts the Trial's verdict only when it is bound to this plan, this
attempt, this Trial and the artifact digest (05 3 "결과 수용"). Terminal status is set once
(CAS on ``rehearsals.status``). The attempt counter lives in the platform DB: three FAIL
verdicts -> NEEDS_HUMAN (FR-5); infrastructure errors have their own cap (07 3).
Preconditions that stopped a Trial before anything ran (``llm_unconfigured`` and friends)
are recorded as INFRA_ERROR with that reason and are not counted. No LLM here (NFR-5).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .config import Settings
from .db import Conn, Database
from .errors import BusinessError
from .events import SYSTEM, Actor, EventKind, EventMode, emit
from .models import RehearsalStart
from .statemachine import PRE_CUTOVER, STAGE_ROLES, State, Trigger, lock_plan, transition
from .upstreams import RehearsalClient, UpstreamError

LOG = logging.getLogger("bravo_api.rehearsal")

# Reasons that mean "nothing was run": not an attempt, not an infrastructure failure.
PRECONDITIONS = frozenset(
    {
        "llm_unconfigured",
        "cluster_unconfigured",
        "rehearsal_unreachable",
        "task_unknown",
        "verifier_unconfigured",
        "tool_layer_unconfigured",
        "role_task_mismatch",
        "role_unknown",
        "role_allowlist_missing",
        "role_allowlist_invalid",
        "unauthorized",
    }
)
OPEN = ("requested", "running", "proposed")


def artifact_digest(artifact: Any) -> str:
    """sha256 over canonical JSON. Must match platform/rehearsal ``artifact.digest``."""
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def bound_problem(body: dict[str, Any], row: dict[str, Any]) -> str | None:
    """Why a terminal Trial result cannot be accepted for this rehearsal row, or None."""
    if body.get("trial_id") != row["trial_id"] or body.get("plan_id") != str(row["plan_id"]):
        return "verdict_unbound:trial"
    if body.get("attempt") != row["attempt"]:
        return "verdict_unbound:attempt"
    verdict = body.get("verdict")
    if verdict == "INFRA_ERROR":
        return None
    artifact, digest = body.get("artifact"), body.get("artifact_digest")
    if artifact is None or not isinstance(digest, str) or artifact_digest(artifact) != digest:
        return "verdict_unbound:artifact"
    v = body.get("verifier")
    if not isinstance(v, dict) or v.get("artifact_digest") != digest:
        return "verdict_unbound:verifier"
    for k in ("run_id", "pod_uid", "image_digest", "task_version", "collected_at"):
        if not isinstance(v.get(k), str) or not v[k]:
            return f"verdict_unbound:verifier_{k}"
    reward = v.get("reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        return "verdict_unbound:reward"
    if (verdict == "PASS") != (reward >= 1):
        return "verdict_unbound:reward_mismatch"
    return None


class RehearsalService:
    def __init__(self, db: Database, client: RehearsalClient | None, settings: Settings) -> None:
        self._db = db
        self._client = client
        self._settings = settings

    async def start(
        self, p: Principal, channel: Channel, plan_id: UUID, req: RehearsalStart, key: str | None
    ) -> dict[str, Any]:
        authorize(p, Action.REHEARSAL_START, channel)
        key = idempotency.validate_key(key)
        digest = idempotency.request_digest(
            {"action": "rehearsal_start", "plan_id": str(plan_id), "task": req.task, "role": req.role}
        )
        if req.role not in STAGE_ROLES[State.PROPOSE.value]:
            raise BusinessError("role_not_allowed_in_stage", status=422)
        actor = Actor.of(p)
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(
                conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, digest=digest, principal_id=p.id
            )
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            state = State(plan["state"])
            if state.value not in PRE_CUTOVER:
                raise BusinessError("rehearsal_not_allowed", detail={"state": state.value})  # S2-AC5
            if plan["active_rehearsal_id"] is not None:
                raise BusinessError("rehearsal_active")
            if self._client is None:  # checked after the state rules so S2-AC5 is reported as such
                raise BusinessError("rehearsal_unconfigured", status=503)
            if state is State.NEEDS_HUMAN:
                if channel is not Channel.CONSOLE:
                    raise BusinessError("needs_human_console_only", status=403)
                plan = await transition(
                    conn,
                    plan_id,
                    Trigger.REPROPOSAL_REQUESTED,
                    actor=actor,
                    reason="human_reset",
                    updates={"rehearsal_failures": 0, "rehearsal_infra_errors": 0},
                )
            elif state is State.GATE:
                if plan["last_gate_verdict_id"] is None:
                    raise BusinessError("gate_not_run")
                plan = await transition(conn, plan_id, Trigger.REPROPOSAL_REQUESTED, actor=actor, reason="gate_block")
            elif state is not State.PROPOSE:
                raise BusinessError("state_mismatch", detail={"state": state.value})
            cur = await conn.execute("SELECT count(*) AS n FROM rehearsals WHERE plan_id = %s", (plan_id,))
            attempt = int((await cur.fetchone() or {"n": 0})["n"]) + 1
            cur = await conn.execute(
                "SELECT attempt, status, reason FROM rehearsals WHERE plan_id = %s AND status = 'FAIL' ORDER BY attempt",
                (plan_id,),
            )
            prior = [{"attempt": r["attempt"], "verdict": r["status"], "reason": r["reason"]} for r in await cur.fetchall()]
            analysis = plan["analysis"]
            rehearsal_id = uuid.uuid4()
            idem = f"rehearsal-{rehearsal_id}"
            await conn.execute(
                """INSERT INTO rehearsals (rehearsal_id, tenant_id, plan_id, attempt, task, role, idem_key, status,
                                           requested_by, channel)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'requested', %s, %s)""",
                (rehearsal_id, p.tenant_id, plan_id, attempt, req.task, req.role, idem, p.id, channel.value),
            )
            await conn.execute(
                "UPDATE plans SET active_rehearsal_id = %s, rehearsal = 'running', version = version + 1, "
                "updated_at = now() WHERE plan_id = %s",
                (rehearsal_id, plan_id),
            )
            await emit(
                conn,
                plan_id=plan_id,
                kind=EventKind.TRIAL_STARTED,
                mode=EventMode.LIVE,
                actor=actor,
                data={
                    "rehearsal_id": str(rehearsal_id),
                    "attempt": attempt,
                    "task": req.task,
                    "role": req.role,
                    "status": "requested",
                },
            )
            response: dict[str, Any] = {
                "plan_id": str(plan_id),
                "rehearsal_id": str(rehearsal_id),
                "attempt": attempt,
                "task": req.task,
                "role": req.role,
                "trial_id": None,
                "status": "requested",
                "state": State.PROPOSE.value,
                "reason": None,
            }
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, response=response, status_code=202
            )

        payload = {
            "plan_id": str(plan_id),
            "tenant_id": p.tenant_id,
            "rehearsal_id": str(rehearsal_id),
            "attempt": attempt,
            "max_attempts": self._settings.rehearsal_max_attempts,
            "task": req.task,
            "role": req.role,
            "prior_failures": prior,
            "analysis": analysis,
        }
        assert self._client is not None
        try:
            started = await self._client.start(payload, idem)
        except UpstreamError as exc:
            reason = exc.reason
            async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
                row = await _row(conn, rehearsal_id)
                plan = await settle(conn, self._settings, row, {"verdict": "INFRA_ERROR", "reason": reason})
                response = {**response, "status": "INFRA_ERROR", "reason": reason, "state": plan["state"]}
                await idempotency.complete(
                    conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, response=response, status_code=202
                )
            return response
        async with self._db.tenant_tx(p.tenant_id, p.id) as conn:
            await conn.execute(
                "UPDATE rehearsals SET trial_id = %s, status = 'running' WHERE rehearsal_id = %s AND status = 'requested'",
                (started["trial_id"], rehearsal_id),
            )
            response = {**response, "trial_id": started["trial_id"], "status": "running"}
            await idempotency.complete(
                conn, tenant_id=p.tenant_id, action="rehearsal_start", key=key, response=response, status_code=202
            )
        return response


async def _row(conn: Conn, rehearsal_id: UUID) -> dict[str, Any]:
    cur = await conn.execute("SELECT * FROM rehearsals WHERE rehearsal_id = %s", (rehearsal_id,))
    row = await cur.fetchone()
    if row is None:
        raise BusinessError("rehearsal_not_found", status=404)
    return row


async def settle(conn: Conn, settings: Settings, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Record a terminal Trial result once and move the plan. Returns the plan row."""
    plan = await lock_plan(conn, row["plan_id"])
    verdict = body.get("verdict")
    reason = body.get("reason") if isinstance(body.get("reason"), str) else None
    if verdict not in ("PASS", "FAIL", "INFRA_ERROR"):
        verdict, reason = "INFRA_ERROR", "verdict_missing"
    cur = await conn.execute(
        """UPDATE rehearsals SET status = %s, reason = %s, artifact_digest = COALESCE(%s, artifact_digest),
                  artifact = COALESCE(%s, artifact), verifier = %s, ended_at = now()
            WHERE rehearsal_id = %s AND status = ANY(%s) RETURNING *""",
        (
            verdict,
            reason,
            body.get("artifact_digest") if verdict != "INFRA_ERROR" else None,
            Jsonb(body["artifact"]) if verdict != "INFRA_ERROR" and body.get("artifact") is not None else None,
            Jsonb(body["verifier"]) if isinstance(body.get("verifier"), dict) else None,
            row["rehearsal_id"],
            list(OPEN),
        ),
    )
    if await cur.fetchone() is None or plan["active_rehearsal_id"] != row["rehearsal_id"]:
        LOG.warning("late or duplicate trial result ignored rehearsal_id=%s", row["rehearsal_id"])
        return plan  # terminal status already set: late results are recorded in logs only
    await emit(
        conn,
        plan_id=row["plan_id"],
        kind=EventKind.TRIAL_VERDICT,
        mode=EventMode.LIVE,
        actor=SYSTEM,
        data={
            "rehearsal_id": str(row["rehearsal_id"]),
            "trial_id": row["trial_id"],
            "attempt": row["attempt"],
            "verdict": verdict,
            "reason": reason,
            "artifact_digest": body.get("artifact_digest") if verdict != "INFRA_ERROR" else None,
            "verifier": body.get("verifier") if isinstance(body.get("verifier"), dict) else None,
        },
    )
    state = State(plan["state"])
    common: dict[str, Any] = {"actor": SYSTEM, "data": {"rehearsal_id": str(row["rehearsal_id"])}}
    base = {"active_rehearsal_id": None, "last_rehearsal_id": row["rehearsal_id"]}
    if state not in (State.PROPOSE, State.REHEARSE):  # pragma: no cover - guarded by active_rehearsal_id
        return plan
    if verdict == "PASS":
        if state is State.PROPOSE:
            plan = await transition(conn, plan["plan_id"], Trigger.PROPOSAL_ACCEPTED, **common)
        return await transition(
            conn,
            plan["plan_id"],
            Trigger.REHEARSAL_PASSED,
            reason=None,
            updates={
                **base,
                "rehearsal": "passed",
                "approved_artifact_digest": body["artifact_digest"],
                "revision": plan["revision"] + 1,
            },  # new content -> new revision (approvals bind it)
            **common,
        )
    if verdict == "FAIL":
        failures = plan["rehearsal_failures"] + 1
        exhausted = failures >= settings.rehearsal_max_attempts
        return await transition(
            conn,
            plan["plan_id"],
            Trigger.ATTEMPTS_EXHAUSTED if exhausted else Trigger.REHEARSAL_FAILED,
            reason="rehearsal_attempts_exhausted" if exhausted else f"rehearsal_failed:{reason or 'verifier'}",
            updates={**base, "rehearsal": "needs_human" if exhausted else "failed", "rehearsal_failures": failures},
            **common,
        )
    infra = plan["rehearsal_infra_errors"] + (0 if reason in PRECONDITIONS else 1)
    exhausted = infra > settings.rehearsal_max_infra_errors
    return await transition(
        conn,
        plan["plan_id"],
        Trigger.ATTEMPTS_EXHAUSTED if exhausted else Trigger.REHEARSAL_INFRA_ERROR,
        reason="rehearsal_infra_errors_exhausted" if exhausted else reason or "infra_error",
        updates={**base, "rehearsal": "needs_human" if exhausted else "infra_error", "rehearsal_infra_errors": infra},
        **common,
    )


class RehearsalWatcher:
    """Polls in-flight Trials (any channel started them) and applies their progress."""

    def __init__(self, db: Database, client: RehearsalClient | None, settings: Settings) -> None:
        self._db = db
        self._client = client
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._client is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="rehearsal-watch")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep watching; the Trial record is the source of truth
                LOG.exception("rehearsal watcher poll failed")
            await asyncio.sleep(self._settings.rehearsal_poll_interval_s)

    async def poll_once(self) -> int:
        assert self._client is not None
        async with self._db.tenant_tx("") as conn:
            cur = await conn.execute("SELECT tenant_id, plan_id, rehearsal_id FROM bravo_inflight_rehearsals()")
            inflight = await cur.fetchall()
        for item in inflight:
            async with self._db.tenant_tx(item["tenant_id"]) as conn:
                row = await _row(conn, item["rehearsal_id"])
            if not row["trial_id"]:
                continue  # the start call settles it
            try:
                body = await self._client.get(row["trial_id"])
            except UpstreamError as exc:
                if exc.reason != "trial_lost":
                    continue  # transient: read again next poll, never invent a result
                body = {"verdict": "INFRA_ERROR", "reason": "trial_lost"}
            async with self._db.tenant_tx(item["tenant_id"]) as conn:
                await self._apply(conn, row, body)
        return len(inflight)

    async def _apply(self, conn: Conn, row: dict[str, Any], body: dict[str, Any]) -> None:
        plan = await lock_plan(conn, row["plan_id"])
        raw_events = body.get("tool_events")
        events: list[dict[str, Any]] = [
            e
            for e in (raw_events if isinstance(raw_events, list) else [])
            if isinstance(e, dict) and isinstance(e.get("seq"), int)
        ]
        last = row["tool_seq"]
        for ev in sorted(events, key=lambda e: e["seq"]):
            if ev["seq"] <= last:
                continue
            last = ev["seq"]
            if not ev.get("allowed"):
                await emit(
                    conn,
                    plan_id=row["plan_id"],
                    kind=EventKind.TOOL_DENIED,
                    mode=EventMode.LIVE,
                    actor=SYSTEM,
                    data={
                        "rehearsal_id": str(row["rehearsal_id"]),
                        "trial_id": row["trial_id"],
                        "seq": ev["seq"],
                        "tool": ev.get("tool"),
                        "rule": ev.get("rule"),
                        "at": ev.get("at"),
                    },
                )
        if last != row["tool_seq"]:
            await conn.execute("UPDATE rehearsals SET tool_seq = %s WHERE rehearsal_id = %s", (last, row["rehearsal_id"]))
        verdict = body.get("verdict")
        if verdict in ("PASS", "FAIL", "INFRA_ERROR"):
            problem = None if verdict == "INFRA_ERROR" and "trial_id" not in body else bound_problem(body, row)
            if problem:
                body = {"verdict": "INFRA_ERROR", "reason": problem}
            await settle(conn, self._settings, row, body)
            return
        if body.get("phase") in ("proposed", "verifying") and row["status"] == "running":
            digest = body.get("artifact_digest")
            if body.get("artifact") is None or not isinstance(digest, str) or artifact_digest(body["artifact"]) != digest:
                return  # not a frozen artifact yet; wait for the terminal result
            await conn.execute(
                "UPDATE rehearsals SET status = 'proposed', artifact_digest = %s, artifact = %s "
                "WHERE rehearsal_id = %s AND status = 'running'",
                (digest, Jsonb(body["artifact"]), row["rehearsal_id"]),
            )
            await emit(
                conn,
                plan_id=row["plan_id"],
                kind=EventKind.TRIAL_PROPOSED,
                mode=EventMode.LIVE,
                actor=SYSTEM,
                data={"rehearsal_id": str(row["rehearsal_id"]), "trial_id": row["trial_id"], "artifact_digest": digest},
            )
            if plan["state"] == State.PROPOSE.value and plan["active_rehearsal_id"] == row["rehearsal_id"]:
                await transition(
                    conn,
                    row["plan_id"],
                    Trigger.PROPOSAL_ACCEPTED,
                    actor=SYSTEM,
                    data={"rehearsal_id": str(row["rehearsal_id"]), "artifact_digest": digest},
                )
