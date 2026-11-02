"""Plan state machine: one transition table, one function (spec §8.2, 03 3).

Full path: COLLECT -> ANALYZE -> (GATE | PROPOSE) ; PROPOSE -> REHEARSE -> GATE ; GATE ->
AWAIT_APPROVAL -> CUTOVER -> WATCH -> (DONE | FAILBACK -> DONE_ROLLED_BACK). A failed
rehearsal returns to PROPOSE until three FAIL verdicts, then NEEDS_HUMAN (FR-5). A gate BLOCK
stays in GATE (a person may fix and re-run, the pilot path) or re-proposes when an operator
starts a rehearsal. CUTOVER failures: ABORT_PRE_WRITE -> AWAIT_APPROVAL, SAFE_HOLD. SHIFT
(edge weights, FR-8) keeps its spec name but has no transition yet: there is no edge
controller contract. Updates are compare-and-swap on ``plans.version`` and every transition
appends a ``state.changed`` event in the same transaction. No LLM (NFR-5).
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
# States that carry a one-time approval request, and the stage it is for.
APPROVAL_STAGE = {"AWAIT_APPROVAL": "cutover", "WATCH": "failback"}


class Trigger(StrEnum):
    COLLECTED = "collected"
    ANALYZED_NO_GAPS = "analyzed_no_gaps"
    ANALYZED_GAPS = "analyzed_gaps"
    REPROPOSAL_REQUESTED = "reproposal_requested"
    PROPOSAL_ACCEPTED = "proposal_accepted"
    REHEARSAL_PASSED = "rehearsal_passed"
    REHEARSAL_FAILED = "rehearsal_failed"
    REHEARSAL_INFRA_ERROR = "rehearsal_infra_error"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    CONTROLS_VERIFIED = "controls_verified"
    CONTROLS_REGRESSED = "controls_regressed"
    FAILBACK_STARTED = "failback_started"
    GATE_PASSED = "gate_passed"
    GATE_BLOCKED = "gate_blocked"
    CUTOVER_STARTED = "cutover_started"
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
    (S.REHEARSE, T.REHEARSAL_PASSED): S.GATE,
    (S.REHEARSE, T.REHEARSAL_FAILED): S.PROPOSE,
    (S.REHEARSE, T.REHEARSAL_INFRA_ERROR): S.PROPOSE,
    (S.REHEARSE, T.ATTEMPTS_EXHAUSTED): S.NEEDS_HUMAN,
    (S.GATE, T.GATE_PASSED): S.AWAIT_APPROVAL,
    (S.NEEDS_HUMAN, T.GATE_PASSED): S.AWAIT_APPROVAL,  # a person fixed it by hand
    # Re-running the gate while awaiting approval: PASS issues a fresh approval request
    # (the old one is void), BLOCK returns to GATE.
    (S.AWAIT_APPROVAL, T.GATE_PASSED): S.AWAIT_APPROVAL,
    (S.AWAIT_APPROVAL, T.GATE_BLOCKED): S.GATE,
    (S.AWAIT_APPROVAL, T.CUTOVER_STARTED): S.CUTOVER,
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
    if target.value in APPROVAL_STAGE:
        if approval_ttl_s is None:
            raise ValueError(f"approval_ttl_s is required when entering {target.value}")
        sets["approval_request_id"] = uuid.uuid4()  # one-time request; any older one is void
        sets["approval_request_expires_at"] = datetime.now(UTC) + timedelta(seconds=approval_ttl_s)
        sets["approval_stage"] = APPROVAL_STAGE[target.value]
    elif current.value in APPROVAL_STAGE:
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
    return updated
