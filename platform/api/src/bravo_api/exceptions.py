"""Control exceptions (spec S1 exception flow 5b, contract 15 §9).

"목적지에서 대체할 수 없는 통제가 있다 -> 승인자가 예외를 승인하면 '예외 승인'으로 따로 표시하고 동등 판정에서 뺀다."

- An operator requests an exception for one row of the plan's last gate verdict: a measured FAIL on the
  destination (source PASS or FAIL) that carries destination evidence. UNKNOWN is never exceptable (5a).
- The exception is bound to plan, control, destination, the measured pair and the destination evidence digest.
- A human approver decides in the console (same channel and role as other approvals). The decider is the
  session principal and differs from the requester and the plan's author; the DB trigger checks it again.
- ``gate_inputs`` / ``record_verdict`` are used by every gate verdict assembly: approved, unexpired exceptions
  turn their matching row into ``EXCEPTION``; exceptions whose control was measured differently become stale.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import errors as pgerrors

from . import idempotency
from .auth import Action, Channel, Principal, authorize
from .db import Conn, Database
from .errors import BusinessError, ForbiddenError, NotFoundError
from .events import Actor, EventKind, EventMode, emit
from .gate import ControlException, GateResult, exception_drift
from .models import ExceptionApprove, ExceptionReject, ExceptionRequest
from .statemachine import lock_plan

MIN_TTL = timedelta(minutes=5)
MAX_TTL = timedelta(days=30)
OPEN = ("requested", "approved")
# No new request or decision once the cutover has started or the plan has ended.
CLOSED_STATES = frozenset({"CUTOVER", "FAILBACK", "DONE", "DONE_ROLLED_BACK", "SAFE_HOLD", "ABORT_PRE_WRITE"})

_VIEW_SQL = """
SELECT e.*, t.name AS target_name, rq.email AS requested_email, dc.email AS decided_email
  FROM control_exceptions e
  JOIN environments t ON t.environment_id = e.target_environment_id
  LEFT JOIN principals rq ON rq.id = e.requested_by
  LEFT JOIN principals dc ON dc.id = e.decided_by
 WHERE e.plan_id = %s {extra}
 ORDER BY e.requested_at DESC, e.exception_id
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def view(row: dict[str, Any], applied_ids: set[str], now: datetime) -> dict[str, Any]:
    status = row["status"]
    if status in OPEN and row["expires_at"] <= now:
        status = "expired"  # not recorded yet; the next request, decision or gate run records it
    return {
        "exception_id": str(row["exception_id"]),
        "plan_id": str(row["plan_id"]),
        "control_id": row["control_id"],
        "target_environment": {"environment_id": str(row["target_environment_id"]), "name": row["target_name"]},
        "gate_verdict_id": str(row["gate_verdict_id"]),
        "src": row["src"],
        "dst": row["dst"],
        "evidence_digest": row["evidence_digest"],
        "reason": row["reason"],
        "compensating_control": row["compensating_control"],
        "expires_at": _iso(row["expires_at"]),
        "status": status,
        "stale_reason": row["stale_reason"],
        "requested_by": row["requested_email"],
        "requested_at": _iso(row["requested_at"]),
        "decided_by": row["decided_email"],
        "decided_at": _iso(row["decided_at"]),
        "decision_note": row["decision_note"],
        "applied": str(row["exception_id"]) in applied_ids,
    }


async def _applied_ids(conn: Conn, plan: dict[str, Any]) -> set[str]:
    if not plan["last_gate_verdict_id"]:
        return set()
    cur = await conn.execute("SELECT exceptions FROM gate_verdicts WHERE verdict_id = %s", (plan["last_gate_verdict_id"],))
    g = await cur.fetchone()
    return {str(x.get("exception_id")) for x in (g["exceptions"] if g else None) or [] if isinstance(x, dict)}


async def _views(conn: Conn, plan: dict[str, Any], exception_id: UUID | None = None) -> list[dict[str, Any]]:
    if exception_id is None:
        cur = await conn.execute(_VIEW_SQL.format(extra=""), (plan["plan_id"],))
    else:
        cur = await conn.execute(_VIEW_SQL.format(extra="AND e.exception_id = %s"), (plan["plan_id"], exception_id))
    rows = await cur.fetchall()
    applied = await _applied_ids(conn, plan)
    now = datetime.now(UTC)
    return [view(r, applied, now) for r in rows]


async def expire_due(conn: Conn, plan_id: UUID, actor: Actor) -> None:
    """Record ``expired`` for open exceptions past ``expires_at`` (one event each)."""
    cur = await conn.execute(
        "UPDATE control_exceptions SET status = 'expired' WHERE plan_id = %s AND status IN ('requested', 'approved') "
        "AND expires_at <= now() RETURNING exception_id, control_id, expires_at",
        (plan_id,),
    )
    for r in await cur.fetchall():
        await emit(conn, plan_id=plan_id, kind=EventKind.EXCEPTION_EXPIRED, mode=EventMode.LIVE, actor=actor,
                   data={"exception_id": str(r["exception_id"]), "control_id": r["control_id"],
                         "expires_at": _iso(r["expires_at"])})


async def gate_inputs(
    conn: Conn, plan: dict[str, Any], actor: Actor
) -> tuple[list[dict[str, Any]], tuple[ControlException, ...]]:
    """Inside the gate transaction (plan row locked): open exceptions of this plan and destination, and the
    approved, unexpired ones as ``evaluate`` inputs. Expired ones are recorded first."""
    await expire_due(conn, plan["plan_id"], actor)
    cur = await conn.execute(
        """SELECT e.exception_id, e.control_id, e.src, e.dst, e.evidence_digest, e.status, e.expires_at,
                  e.compensating_control, dc.email AS decided_email
             FROM control_exceptions e LEFT JOIN principals dc ON dc.id = e.decided_by
            WHERE e.plan_id = %s AND e.target_environment_id = %s AND e.status IN ('requested', 'approved')
              AND e.expires_at > now()
            ORDER BY e.control_id""",
        (plan["plan_id"], plan["target_environment_id"]),
    )
    rows = await cur.fetchall()
    effective = tuple(
        ControlException(str(r["exception_id"]), r["control_id"], r["src"], r["dst"], r["evidence_digest"],
                         _iso(r["expires_at"]), r["decided_email"], r["compensating_control"])
        for r in rows if r["status"] == "approved"
    )
    return [dict(r) for r in rows], effective


async def record_verdict(
    conn: Conn, plan_id: UUID, verdict_id: UUID, result: GateResult, open_rows: list[dict[str, Any]], actor: Actor
) -> None:
    """After a verdict is stored: exceptions whose control was measured differently become ``stale``."""
    for ex, cause, now in exception_drift(result, open_rows):
        await conn.execute(
            "UPDATE control_exceptions SET status = 'stale', stale_reason = %s WHERE exception_id = %s "
            "AND status IN ('requested', 'approved')",
            (cause, ex["exception_id"]),
        )
        await emit(conn, plan_id=plan_id, kind=EventKind.EXCEPTION_STALE, mode=EventMode.LIVE, actor=actor, data={
            "exception_id": str(ex["exception_id"]), "control_id": ex["control_id"], "cause": cause,
            "verdict_id": str(verdict_id),
            "was": {"src": ex["src"], "dst": ex["dst"], "evidence_digest": ex["evidence_digest"]}, "now": now,
        })


def _row_of(controls: Any, control_id: str) -> dict[str, Any] | None:
    for c in controls or []:
        if isinstance(c, dict) and c.get("control_id") == control_id:
            return c
    return None


def _drift_cause(row: dict[str, Any] | None, src: str, dst: str, digest: str) -> str | None:
    """Why the last verdict's row no longer supports this exception, or None if it still does."""
    if row is None or "UNKNOWN" in (row.get("src"), row.get("dst")):
        return "not_measured"
    if (row.get("src"), row.get("dst")) != (src, dst):
        return "verdict_changed"
    if row.get("evidence_digest") != digest:
        return "evidence_changed"
    return None


def _db_error(exc: pgerrors.CheckViolation) -> BusinessError | ForbiddenError:
    msg = str(exc.diag.message_primary or "")
    if msg == "separation_of_duties":
        return BusinessError("separation_of_duties", status=403)
    if msg == "exception_decider_not_approver":
        return ForbiddenError("insufficient_scope")
    if msg == "exception_stale":
        return BusinessError("exception_stale", detail={"cause": "db_recheck"})
    if msg == "exception_expired":
        return BusinessError("exception_stale", detail={"cause": "exception_expired"})
    return BusinessError(msg or "exception_rejected_by_db")


class ExceptionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list(self, p: Principal, channel: Channel, plan_id: UUID) -> dict[str, Any]:
        authorize(p, Action.PLAN_READ, channel)
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            cur = await conn.execute("SELECT plan_id, last_gate_verdict_id FROM plans WHERE plan_id = %s", (plan_id,))
            plan = await cur.fetchone()
            if plan is None:
                raise NotFoundError("plan")
            return {"plan_id": str(plan_id), "exceptions": await _views(conn, plan)}

    async def request(
        self, p: Principal, channel: Channel, plan_id: UUID, req: ExceptionRequest, key: str | None
    ) -> dict[str, Any]:
        authorize(p, Action.EXCEPTION_REQUEST, channel)
        key = idempotency.validate_key(key)
        now = datetime.now(UTC)
        if not now + MIN_TTL <= req.expires_at <= now + MAX_TTL:
            raise BusinessError("exception_expiry_invalid", status=422,
                                detail={"min": _iso(now + MIN_TTL), "max": _iso(now + MAX_TTL)})
        digest = idempotency.request_digest({"action": "exception_request", "plan_id": str(plan_id),
                                             **req.model_dump(mode="json")})
        actor = Actor.of(p)
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(conn, tenant_id=p.tenant_id, action="exception_request", key=key,
                                             digest=digest, principal_id=p.id)
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            if plan["state"] in CLOSED_STATES:
                raise BusinessError("state_mismatch", detail={"state": plan["state"]})
            if not plan["last_gate_verdict_id"]:
                raise BusinessError("exception_stale", detail={"cause": "no_gate_verdict"})
            if req.gate_verdict_id != plan["last_gate_verdict_id"]:
                raise BusinessError("exception_stale", detail={"cause": "gate_verdict_superseded"})
            cur = await conn.execute("SELECT controls, evidence_expires_at FROM gate_verdicts WHERE verdict_id = %s",
                                     (plan["last_gate_verdict_id"],))
            g = await cur.fetchone()
            row = _row_of(g["controls"] if g else None, req.control_id)
            if row is None:
                raise BusinessError("exception_not_allowed", 422, {"cause": "control_not_in_verdict"})
            if "UNKNOWN" in (row.get("src"), row.get("dst")) or row.get("src") not in ("PASS", "FAIL"):
                raise BusinessError("exception_not_allowed", 422, {"cause": "unknown"})  # 5a: UNKNOWN blocks
            if row.get("dst") != "FAIL":
                raise BusinessError("exception_not_allowed", 422, {"cause": "not_failed"})
            if not row.get("evidence_digest"):
                raise BusinessError("exception_not_allowed", 422, {"cause": "evidence_missing"})
            if row["evidence_digest"] != req.evidence_digest:
                raise BusinessError("exception_stale", detail={"cause": "evidence_changed"})
            if g is None or g["evidence_expires_at"] is None or g["evidence_expires_at"] <= now:
                raise BusinessError("exception_stale", detail={"cause": "evidence_expired"})  # §8.5
            await expire_due(conn, plan_id, actor)
            cur = await conn.execute(
                "SELECT exception_id, status FROM control_exceptions WHERE plan_id = %s AND control_id = %s "
                "AND status IN ('requested', 'approved')",
                (plan_id, req.control_id),
            )
            existing = await cur.fetchone()
            if existing is not None:
                raise BusinessError("exception_open", detail={"exception_id": str(existing["exception_id"]),
                                                              "status": existing["status"]})
            exception_id = uuid.uuid4()
            try:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO control_exceptions (exception_id, tenant_id, plan_id, control_id,
                                                           target_environment_id, gate_verdict_id, src, dst,
                                                           evidence_digest, reason, compensating_control,
                                                           expires_at, requested_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (exception_id, p.tenant_id, plan_id, req.control_id, plan["target_environment_id"],
                         plan["last_gate_verdict_id"], row["src"], row["dst"], row["evidence_digest"],
                         req.reason.strip(), req.compensating_control.strip(), req.expires_at, p.id),
                    )
            except pgerrors.UniqueViolation as exc:
                raise BusinessError("exception_open") from exc
            except pgerrors.CheckViolation as exc:
                raise _db_error(exc) from exc
            await emit(conn, plan_id=plan_id, kind=EventKind.EXCEPTION_REQUESTED, mode=EventMode.LIVE, actor=actor,
                       data={"exception_id": str(exception_id), "control_id": req.control_id,
                             "target_environment_id": str(plan["target_environment_id"]),
                             "gate_verdict_id": str(plan["last_gate_verdict_id"]), "src": row["src"],
                             "dst": row["dst"], "evidence_digest": row["evidence_digest"],
                             "expires_at": _iso(req.expires_at)})
            result = (await _views(conn, plan, exception_id))[0]
            await idempotency.complete(conn, tenant_id=p.tenant_id, action="exception_request", key=key,
                                       response=result, status_code=201)
            return result

    async def approve(
        self, p: Principal, channel: Channel, plan_id: UUID, exception_id: UUID, req: ExceptionApprove, key: str | None
    ) -> dict[str, Any]:
        return await self._decide(p, channel, plan_id, exception_id, "approved", req.note, req.evidence_digest, key)

    async def reject(
        self, p: Principal, channel: Channel, plan_id: UUID, exception_id: UUID, req: ExceptionReject, key: str | None
    ) -> dict[str, Any]:
        return await self._decide(p, channel, plan_id, exception_id, "rejected", req.note, None, key)

    async def _decide(
        self, p: Principal, channel: Channel, plan_id: UUID, exception_id: UUID, decision: str, note: str | None,
        seen_digest: str | None, key: str | None,
    ) -> dict[str, Any]:
        authorize(p, Action.APPROVE, channel)  # console approver only, like the other approvals
        key = idempotency.validate_key(key)
        action = "exception_approve" if decision == "approved" else "exception_reject"
        digest = idempotency.request_digest({"action": action, "plan_id": str(plan_id), "exception_id": str(exception_id),
                                             "note": note, "evidence_digest": seen_digest})
        async with self.db.tenant_tx(p.tenant_id, p.id) as conn:
            replay = await idempotency.claim(conn, tenant_id=p.tenant_id, action=action, key=key, digest=digest,
                                             principal_id=p.id)
            if replay is not None:
                return replay
            plan = await lock_plan(conn, plan_id)
            cur = await conn.execute(
                "SELECT * FROM control_exceptions WHERE exception_id = %s AND plan_id = %s FOR UPDATE",
                (exception_id, plan_id),
            )
            ex = await cur.fetchone()
            if ex is None:
                raise NotFoundError("exception")
            if plan["state"] in CLOSED_STATES:
                raise BusinessError("state_mismatch", detail={"state": plan["state"]})
            if ex["status"] != "requested":
                raise BusinessError("exception_not_pending", detail={"status": ex["status"]})
            if ex["expires_at"] <= datetime.now(UTC):
                raise BusinessError("exception_stale", detail={"cause": "exception_expired"})
            if p.id in (ex["requested_by"], plan["created_by"]):
                raise BusinessError("separation_of_duties", status=403)
            if decision == "approved":
                if seen_digest != ex["evidence_digest"]:
                    raise BusinessError("exception_stale", detail={"cause": "evidence_mismatch"})
                cur = await conn.execute(
                    "SELECT controls, evidence_expires_at FROM gate_verdicts WHERE verdict_id = %s",
                    (plan["last_gate_verdict_id"],),
                )
                g = await cur.fetchone()
                cause = _drift_cause(_row_of(g["controls"] if g else None, ex["control_id"]), ex["src"], ex["dst"],
                                     ex["evidence_digest"])
                if cause is not None:
                    raise BusinessError("exception_stale", detail={"cause": cause})
                if g is None or g["evidence_expires_at"] is None or g["evidence_expires_at"] <= datetime.now(UTC):
                    raise BusinessError("exception_stale", detail={"cause": "evidence_expired"})  # §8.5
            try:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE control_exceptions SET status = %s, decided_by = %s, decision_note = %s "
                        "WHERE exception_id = %s",
                        (decision, p.id, (note or "").strip() or None, exception_id),
                    )
            except pgerrors.CheckViolation as exc:
                raise _db_error(exc) from exc
            data: dict[str, Any] = {"exception_id": str(exception_id), "control_id": ex["control_id"]}
            if decision == "approved":
                data.update(evidence_digest=ex["evidence_digest"], expires_at=_iso(ex["expires_at"]))
            await emit(conn, plan_id=plan_id, mode=EventMode.LIVE, actor=Actor.of(p), data=data,
                       kind=EventKind.EXCEPTION_APPROVED if decision == "approved" else EventKind.EXCEPTION_REJECTED)
            result = (await _views(conn, plan, exception_id))[0]
            await idempotency.complete(conn, tenant_id=p.tenant_id, action=action, key=key, response=result,
                                       status_code=200)
            return result
