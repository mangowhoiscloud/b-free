"""A2: the Executor refuses runs whose approval is not the platform's real, matching record."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.approvals import ApprovalCheck, ApprovalRejected, PlatformDbApprovals
from bravo_executor.ledger import PostgresLedger, RunRequest

FIXTURE = Path(__file__).parent / "sql" / "approvals_contract.sql"


async def _approval(pool: AsyncConnectionPool[AsyncConnection], plan: uuid.UUID, stage: str = "cutover",
                    same_person: bool = False, expires: str = "10 minutes", started: bool = True,
                    starter_is_approver: bool = False) -> uuid.UUID:
    """An approval row as platform/api leaves it after the console start (consumed by the operator)."""
    aid, approver = uuid.uuid4(), uuid.uuid4()
    starter = approver if starter_is_approver else uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(FIXTURE.read_text(encoding="utf-8").encode())
        await conn.execute(
            "INSERT INTO approvals (approval_id, plan_id, stage, approver_id, requester_id, expires_at, consumed_at, "
            "consumed_by) VALUES (%s, %s, %s, %s, %s, now() + %s::interval, "
            "CASE WHEN %s THEN now() END, CASE WHEN %s THEN %s::uuid END)",
            (aid, plan, stage, approver, approver if same_person else uuid.uuid4(), expires, started, started,
             starter),
        )
    return aid


def _check(aid: uuid.UUID | str, plan: uuid.UUID, kind: str = "cutover", key: str = "k",
           run_kind: str = "") -> ApprovalCheck:
    return ApprovalCheck(approval_id=str(aid), plan_id=plan, kind=kind, idem_key=key, run_kind=run_kind)


async def _run_with(pool: AsyncConnectionPool[AsyncConnection], plan: uuid.UUID, aid: uuid.UUID, kind: str,
                    key: str, status: str) -> uuid.UUID:
    from datetime import UTC, datetime

    ledger = PostgresLedger(pool)
    run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=plan, epoch=1, idem_key=key, kind=kind))
    await ledger.record_stage(run_id, 1, "front.preflight" if kind == "shift" else "1_preflight", datetime.now(UTC),
                              1, "OK", {"approval_id": str(aid)})
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET status = %s WHERE run_id = %s", (status, run_id))
    return run_id


async def test_valid_approval_passes_with_evidence(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    aid = await _approval(pool, plan)
    evidence = await PlatformDbApprovals(pool).verify(_check(aid, plan, run_kind="shift"))
    assert evidence["approval_id"] == str(aid) and evidence["source"] == "platform-db approvals"
    assert evidence["started_by"] is not None


async def test_cutover_needs_the_console_start_and_a_done_shift(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    approvals = PlatformDbApprovals(pool)
    for kwargs, code in (({"started": False}, "APPROVAL_NOT_STARTED"),
                         ({"starter_is_approver": True}, "STARTER_IS_APPROVER")):
        aid = await _approval(pool, plan, **kwargs)  # type: ignore[arg-type]
        with pytest.raises(ApprovalRejected) as info:
            await approvals.verify(_check(aid, plan, run_kind="shift"))
        assert info.value.code == code
    aid = await _approval(pool, plan)
    with pytest.raises(ApprovalRejected) as info:
        await approvals.verify(_check(aid, plan, key=f"cutover-{aid}"))
    assert info.value.code == "SHIFT_NOT_DONE"
    await _run_with(pool, plan, aid, "shift", f"shift-{aid}", "ABORT_PRE_WRITE")
    with pytest.raises(ApprovalRejected) as info:
        await approvals.verify(_check(aid, plan, key=f"cutover-{aid}"))
    assert info.value.code == "SHIFT_NOT_DONE" and info.value.detail["shift_status"] == "ABORT_PRE_WRITE"
    aid2 = await _approval(pool, plan)
    shift_run = await _run_with(pool, plan, aid2, "shift", f"shift-{aid2}", "DONE")
    # The shift run used the approval; the cutover (another kind) may still use it, once.
    evidence = await approvals.verify(_check(aid2, plan, key=f"cutover-{aid2}"))
    assert evidence["shift_run_id"] == str(shift_run)
    await _run_with(pool, plan, aid2, "cutover", f"cutover-{aid2}", "RUNNING")
    with pytest.raises(ApprovalRejected) as info:
        await approvals.verify(_check(aid2, plan, key="another-cutover"))
    assert info.value.code == "APPROVAL_ALREADY_USED"


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"other_plan": True}, "APPROVAL_PLAN_MISMATCH"),
        ({"stage": "failback"}, "APPROVAL_STAGE_MISMATCH"),
        ({"same_person": True}, "APPROVER_IS_REQUESTER"),
        ({"expires": "-1 minute"}, "APPROVAL_EXPIRED"),
    ],
)
async def test_mismatching_approvals_are_refused(
    pool: AsyncConnectionPool[AsyncConnection], setup: dict[str, object], code: str
) -> None:
    plan = uuid.uuid4()
    aid = await _approval(pool, uuid.uuid4() if setup.get("other_plan") else plan,
                          stage=str(setup.get("stage", "cutover")), same_person=bool(setup.get("same_person")),
                          expires=str(setup.get("expires", "10 minutes")))
    with pytest.raises(ApprovalRejected) as info:
        await PlatformDbApprovals(pool).verify(_check(aid, plan, run_kind="shift"))
    assert info.value.code == code


async def test_unknown_or_fake_ids_are_refused(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    await _approval(pool, plan)
    for fake, code in (("smoke-approval", "APPROVAL_ID_INVALID"), (uuid.uuid4(), "APPROVAL_NOT_FOUND")):
        with pytest.raises(ApprovalRejected) as info:
            await PlatformDbApprovals(pool).verify(_check(fake, plan))
        assert info.value.code == code


async def test_one_approval_one_run_per_kind(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    aid = await _approval(pool, plan, stage="failback")
    await _run_with(pool, plan, aid, "failback", "first", "RUNNING")
    await PlatformDbApprovals(pool).verify(_check(aid, plan, kind="failback", key="first"))  # replay is fine
    with pytest.raises(ApprovalRejected) as info:
        await PlatformDbApprovals(pool).verify(_check(aid, plan, kind="failback", key="second"))
    assert info.value.code == "APPROVAL_ALREADY_USED"


async def test_missing_approvals_table_is_refused_not_skipped(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    with pytest.raises(ApprovalRejected) as info:
        await PlatformDbApprovals(pool).verify(_check(uuid.uuid4(), uuid.uuid4()))
    assert info.value.code == "APPROVAL_SOURCE_UNAVAILABLE"
