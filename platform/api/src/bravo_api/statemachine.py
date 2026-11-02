"""Plan state machine: one transition table, one function (spec §8.2, 03 3).

Full path: COLLECT -> ANALYZE -> (GATE | PROPOSE) ; PROPOSE -> REHEARSE -> GATE ; GATE ->
AWAIT_APPROVAL -> SHIFT -> CUTOVER -> WATCH -> (DONE | FAILBACK -> DONE_ROLLED_BACK).
Spec r2 (§8.2 REHEARSE row): a passing rehearsal stays in REHEARSE and opens a change (T31). The pipeline
prepares a candidate (image digest, SBOM, scan); a ``change`` approval binds diff, image digest and policy
diff; the pipeline applies it to the destination; only ``CHANGE_APPLIED`` (applied digest == approved
digest) leaves REHEARSE for GATE, where the system re-runs the gate. Approvals: change (REHEARSE),
cutover (AWAIT_APPROVAL), failback (WATCH). A failed
rehearsal returns to PROPOSE until three FAIL verdicts, then NEEDS_HUMAN (FR-5). A gate BLOCK
stays in GATE (a person may fix and re-run, the pilot path) or re-proposes when an operator
starts a rehearsal. SHIFT (FR-8, contract 15 §10): the operator's console start consumes the
cutover approval and the Executor walks Edge weights 10/50/100 % judged on real chat writes; DONE
-> CUTOVER with the same approval, a revert to 0 % (S3 1a) -> AWAIT_APPROVAL with a fresh request,
an unconfirmed weight change -> SAFE_HOLD. There is no AWAIT_APPROVAL -> CUTOVER shortcut.
CUTOVER failures: ABORT_PRE_WRITE -> AWAIT_APPROVAL, SAFE_HOLD. Updates are compare-and-swap on
``plans.version`` and every transition appends a ``state.changed`` event in the same transaction.
No LLM (NFR-5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from .db import Conn
from .errors import BusinessError, NotFoundError
from .events import Actor, EventKind, EventMode, emit


class State(StrEnum):
    COLLECT = "COLLECT"
    ANALYZE = "ANALYZE"
    PROPOSE = "PROPOSE"
    REHEARSE = "REHEARSE"
    GATE = "GATE"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"
    SHIFT = "SHIFT"
    CUTOVER = "CUTOVER"
    WATCH = "WATCH"
    DONE = "DONE"
    FAILBACK = "FAILBACK"
    DONE_ROLLED_BACK = "DONE_ROLLED_BACK"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    ABORT_PRE_WRITE = "ABORT_PRE_WRITE"
    SAFE_HOLD = "SAFE_HOLD"


# Rehearsal is allowed only before cutover is committed (§8.4, S2-AC5).
PRE_CUTOVER = frozenset({"COLLECT", "ANALYZE", "PROPOSE", "REHEARSE", "GATE", "AWAIT_APPROVAL", "NEEDS_HUMAN"})
# Agent roles each stage may invoke (coordination note 2026-11-02). Agents only propose; the
# gate and the Executor decide. ANALYZE roles produce readiness proposals; PROPOSE/REHEARSE
# roles produce changes that go through the rehearsal verifier; ``report`` is P1.
STAGE_ROLES: dict[str, frozenset[str]] = {
    "ANALYZE": frozenset({"discovery", "db", "governance"}),
    "PROPOSE": frozenset({"governance", "app_migration", "ci"}),
    "REHEARSE": frozenset({"governance", "app_migration", "ci"}),
}
# States that carry a one-time approval request on entry, and the stage it is for. The ``change`` request
# is issued inside REHEARSE when the T31 candidate is prepared (``request_approval``), not on entry.
APPROVAL_STAGE = {"AWAIT_APPROVAL": "cutover", "WATCH": "failback"}
APPROVAL_STAGES = ("change", "cutover", "failback")


class Trigger(StrEnum):
    COLLECTED = "collected"
    ANALYZED_NO_GAPS = "analyzed_no_gaps"
    ANALYZED_GAPS = "analyzed_gaps"
    REPROPOSAL_REQUESTED = "reproposal_requested"
    PROPOSAL_ACCEPTED = "proposal_accepted"
    REHEARSAL_PASSED = "rehearsal_passed"
    CHANGE_APPLIED = "change_applied"
    REHEARSAL_FAILED = "rehearsal_failed"
    REHEARSAL_INFRA_ERROR = "rehearsal_infra_error"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    CONTROLS_VERIFIED = "controls_verified"
    CONTROLS_REGRESSED = "controls_regressed"
    FAILBACK_STARTED = "failback_started"
    GATE_PASSED = "gate_passed"
    GATE_BLOCKED = "gate_blocked"
    SHIFT_STARTED = "shift_started"
    SHIFT_DONE = "shift_done"
    SHIFT_REVERTED = "shift_reverted"
    RUN_DONE = "run_done"
    RUN_ABORTED_PRE_WRITE = "run_aborted_pre_write"
    ABORT_SETTLED = "abort_settled"
    RUN_SAFE_HOLD = "run_safe_hold"


S, T = State, Trigger
TRANSITIONS: dict[tuple[State, Trigger], State] = {
    (S.COLLECT, T.COLLECTED): S.ANALYZE,
    # ANALYZE: code-change (or unclassified) dependencies need a proposal; otherwise the gate
    # runs first and its BLOCK reasons are the gaps (S1 -> S2).
    (S.ANALYZE, T.ANALYZED_NO_GAPS): S.GATE,
    (S.ANALYZE, T.ANALYZED_GAPS): S.PROPOSE,
    (S.GATE, T.REPROPOSAL_REQUESTED): S.PROPOSE,
    (S.NEEDS_HUMAN, T.REPROPOSAL_REQUESTED): S.PROPOSE,  # a person reset the attempt budget
    # PROPOSE -> REHEARSE once plan.propose passed schema and policy lint inside the Trial.
    (S.PROPOSE, T.PROPOSAL_ACCEPTED): S.REHEARSE,
    (S.PROPOSE, T.REHEARSAL_FAILED): S.PROPOSE,  # no valid proposal: counted, re-propose
    (S.PROPOSE, T.REHEARSAL_INFRA_ERROR): S.PROPOSE,  # not counted as an attempt
    (S.PROPOSE, T.ATTEMPTS_EXHAUSTED): S.NEEDS_HUMAN,
    # r2: the verifier passed -> stay in REHEARSE; a change (T31) is opened and needs the change approval.
    (S.REHEARSE, T.REHEARSAL_PASSED): S.REHEARSE,
    # The approved change was applied to the destination with the approved digest -> GATE re-check.
    (S.REHEARSE, T.CHANGE_APPLIED): S.GATE,
    (S.REHEARSE, T.REHEARSAL_FAILED): S.PROPOSE,
    (S.REHEARSE, T.REHEARSAL_INFRA_ERROR): S.PROPOSE,
    (S.REHEARSE, T.ATTEMPTS_EXHAUSTED): S.NEEDS_HUMAN,
    (S.GATE, T.GATE_PASSED): S.AWAIT_APPROVAL,
    (S.NEEDS_HUMAN, T.GATE_PASSED): S.AWAIT_APPROVAL,  # a person fixed it by hand
    # Re-running the gate while awaiting approval: PASS issues a fresh approval request
    # (the old one is void), BLOCK returns to GATE.
    (S.AWAIT_APPROVAL, T.GATE_PASSED): S.AWAIT_APPROVAL,
    (S.AWAIT_APPROVAL, T.GATE_BLOCKED): S.GATE,
    # SHIFT (contract 15 §10): console start -> 10/50/100 judged -> CUTOVER, or 0 % and back (S3 1a).
    (S.AWAIT_APPROVAL, T.SHIFT_STARTED): S.SHIFT,
    (S.SHIFT, T.SHIFT_DONE): S.CUTOVER,
    (S.SHIFT, T.SHIFT_REVERTED): S.AWAIT_APPROVAL,  # fresh cutover request; the consumed one is void
    (S.SHIFT, T.RUN_SAFE_HOLD): S.SAFE_HOLD,
    (S.CUTOVER, T.RUN_DONE): S.WATCH,
    (S.CUTOVER, T.RUN_ABORTED_PRE_WRITE): S.ABORT_PRE_WRITE,
    (S.ABORT_PRE_WRITE, T.ABORT_SETTLED): S.AWAIT_APPROVAL,
    (S.CUTOVER, T.RUN_SAFE_HOLD): S.SAFE_HOLD,
    # WATCH: the control re-test (same rules as the gate) closes the move or keeps watching.
    (S.WATCH, T.CONTROLS_VERIFIED): S.DONE,
    (S.WATCH, T.CONTROLS_REGRESSED): S.WATCH,  # fresh failback approval request
    (S.WATCH, T.FAILBACK_STARTED): S.FAILBACK,
    (S.FAILBACK, T.RUN_DONE): S.DONE_ROLLED_BACK,
    (S.FAILBACK, T.RUN_ABORTED_PRE_WRITE): S.WATCH,  # nothing written on the source: target stays primary
    (S.FAILBACK, T.RUN_SAFE_HOLD): S.SAFE_HOLD,
}

# Columns a transition may set besides state. Anything else is a programming error.
_SETTABLE = frozenset(
    {
        "last_gate_verdict_id",
        "approval_requested_by",
        "active_run_id",
        "last_run_id",
        "epoch",
        "collect_snapshot",
        "collected_at",
        "analysis",
        "analyzed_at",
        "active_rehearsal_id",
        "last_rehearsal_id",
        "rehearsal",
        "rehearsal_failures",
        "rehearsal_infra_errors",
        "approved_artifact_digest",
        "revision",
        "active_change_id",
        "last_change_id",
        "applied_image_digest",
        "write_pause_budget_ms",
    }
)


def next_state(current: State, trigger: Trigger) -> State:
    target = TRANSITIONS.get((current, trigger))
    if target is None:
        raise BusinessError("state_mismatch", status=409, detail={"state": current.value, "trigger": trigger.value})
    return target


async def lock_plan(conn: Conn, plan_id: UUID) -> dict[str, Any]:
    cur = await conn.execute("SELECT * FROM plans WHERE plan_id = %s FOR UPDATE", (plan_id,))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("plan")
    return row


async def transition(
    conn: Conn,
    plan_id: UUID,
    trigger: Trigger,
    *,
    actor: Actor,
    mode: EventMode = EventMode.LIVE,
    expected_version: int | None = None,
    reason: str | None = None,
    data: dict[str, Any] | None = None,
    run_id: UUID | None = None,
    updates: dict[str, Any] | None = None,
    approval_ttl_s: int | None = None,
) -> dict[str, Any]:
    """Apply ``trigger`` to the plan under a row lock with a CAS on ``version``.

    Entering AWAIT_APPROVAL (stage cutover) or WATCH (stage failback) issues a fresh one-time
    approval request that expires after ``approval_ttl_s`` (required for those transitions).
    """
    plan = await lock_plan(conn, plan_id)
    if expected_version is not None and plan["version"] != expected_version:
        raise BusinessError("version_conflict", detail={"version": plan["version"]})
    current = State(plan["state"])
    target = next_state(current, trigger)

    sets: dict[str, Any] = {k: Jsonb(v) if isinstance(v, dict) else v for k, v in (updates or {}).items()}
    unknown = set(sets) - _SETTABLE
    if unknown:
        raise ValueError(f"transition cannot set {sorted(unknown)}")
    issued: dict[str, Any] | None = None
    if target.value in APPROVAL_STAGE:
        if approval_ttl_s is None:
            raise ValueError(f"approval_ttl_s is required when entering {target.value}")
        sets["approval_request_id"] = uuid.uuid4()  # one-time request; any older one is void
        sets["approval_request_expires_at"] = datetime.now(UTC) + timedelta(seconds=approval_ttl_s)
        sets["approval_stage"] = APPROVAL_STAGE[target.value]
        issued = {"stage": sets["approval_stage"], "approval_request_id": str(sets["approval_request_id"]),
                  "expires_at": sets["approval_request_expires_at"].isoformat()}
    elif current.value in APPROVAL_STAGE or (plan.get("approval_stage") == "change" and target is not current):
        sets["approval_request_id"] = None
        sets["approval_request_expires_at"] = None
        sets["approval_stage"] = None
    sets["state"] = target.value
    sets["state_reason"] = reason

    assignments = sql.SQL(", ").join(sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in sets)
    query = sql.SQL(
        "UPDATE plans SET {assignments}, version = version + 1, updated_at = now() "
        "WHERE plan_id = {plan_id} AND version = {version} RETURNING *"
    ).format(assignments=assignments, plan_id=sql.Placeholder("plan_id"), version=sql.Placeholder("version"))
    params = {**sets, "plan_id": plan_id, "version": plan["version"]}
    cur = await conn.execute(query, params)
    updated = await cur.fetchone()
    if updated is None:  # pragma: no cover - the row lock makes this unreachable
        raise BusinessError("version_conflict")

    await emit(
        conn,
        plan_id=plan_id,
        kind=EventKind.STATE_CHANGED,
        mode=mode,
        actor=actor,
        run_id=run_id,
        data={"from": current.value, "to": target.value, "trigger": trigger.value, "reason": reason, **(data or {})},
    )
    if issued is not None:
        await emit(
            conn,
            plan_id=plan_id,
            kind=EventKind.APPROVAL_REQUESTED,
            mode=mode,
            actor=actor,
            run_id=run_id,
            data={**issued, "revision": updated["revision"], **approval_binding(updated)},
        )
    return updated


def approval_binding(plan: dict[str, Any]) -> dict[str, Any]:
    """What a cutover / failback request binds besides the revision (shown before approval)."""
    if plan.get("approval_stage") == "cutover":
        return {
            "gate_verdict_id": str(plan["last_gate_verdict_id"]) if plan.get("last_gate_verdict_id") else None,
            "artifact_digest": plan.get("approved_artifact_digest"),
            "applied_image_digest": plan.get("applied_image_digest"),
        }
    if plan.get("approval_stage") == "failback":
        return {"bound_run_id": str(plan["last_run_id"]) if plan.get("last_run_id") else None}
    return {}


async def request_change_approval(
    conn: Conn, plan_id: UUID, change: dict[str, Any], *, ttl_s: int, actor: Actor
) -> dict[str, Any]:
    """Issue the one-time ``change`` approval request for a prepared change. The plan stays in REHEARSE.

    Binds: plan revision, change id, artifact (rehearsal proposal) digest, diff digest, image digest, policy
    diff digest. The requester is the operator who asked for the change (separation of duties, D-18).
    """
    plan = await lock_plan(conn, plan_id)
    if plan["state"] != State.REHEARSE.value or plan["active_change_id"] != change["change_id"]:
        raise BusinessError("state_mismatch", detail={"state": plan["state"]})
    request_id = uuid.uuid4()
    expires = datetime.now(UTC) + timedelta(seconds=ttl_s)
    cur = await conn.execute(
        """UPDATE plans SET approval_request_id = %s, approval_request_expires_at = %s, approval_stage = 'change',
                  approval_requested_by = %s, version = version + 1, updated_at = now()
            WHERE plan_id = %s RETURNING *""",
        (request_id, expires, change["requested_by"], plan_id),
    )
    updated = await cur.fetchone()
    await conn.execute(
        "UPDATE changes SET approval_request_id = %s WHERE change_id = %s", (request_id, change["change_id"])
    )
    await emit(
        conn,
        plan_id=plan_id,
        kind=EventKind.APPROVAL_REQUESTED,
        mode=EventMode.LIVE,
        actor=actor,
        data={
            "stage": "change",
            "approval_request_id": str(request_id),
            "expires_at": expires.isoformat(),
            "revision": updated["revision"] if updated else plan["revision"],
            "change_id": str(change["change_id"]),
            "artifact_digest": change["artifact_digest"],
            "diff_digest": change["diff_digest"],
            "image_digest": change["image_digest"],
            "policy_diff_digest": change["policy_diff_digest"],
        },
    )
    return updated or plan
