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
                    same_person: bool = False, expires: str = "10 minutes") -> uuid.UUID:
    aid, approver = uuid.uuid4(), uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(FIXTURE.read_text(encoding="utf-8").encode())
        await conn.execute(
            "INSERT INTO approvals (approval_id, plan_id, stage, approver_id, requester_id, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, now() + %s::interval)",
            (aid, plan, stage, approver, approver if same_person else uuid.uuid4(), expires),
        )
    return aid


def _check(aid: uuid.UUID | str, plan: uuid.UUID, kind: str = "cutover", key: str = "k") -> ApprovalCheck:
    return ApprovalCheck(approval_id=str(aid), plan_id=plan, kind=kind, idem_key=key)


async def test_valid_approval_passes_with_evidence(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    aid = await _approval(pool, plan)
    evidence = await PlatformDbApprovals(pool).verify(_check(aid, plan))
    assert evidence["approval_id"] == str(aid) and evidence["source"] == "platform-db approvals"


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
        await PlatformDbApprovals(pool).verify(_check(aid, plan))
    assert info.value.code == code


async def test_unknown_or_fake_ids_are_refused(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    await _approval(pool, plan)
    for fake, code in (("smoke-approval", "APPROVAL_ID_INVALID"), (uuid.uuid4(), "APPROVAL_NOT_FOUND")):
        with pytest.raises(ApprovalRejected) as info:
            await PlatformDbApprovals(pool).verify(_check(fake, plan))
        assert info.value.code == code


async def test_one_approval_one_run(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    plan = uuid.uuid4()
    aid = await _approval(pool, plan)
    ledger = PostgresLedger(pool)
    run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=plan, epoch=1, idem_key="first"))
    from datetime import UTC, datetime
    await ledger.record_stage(run_id, 1, "1_preflight", datetime.now(UTC), 1, "OK", {"approval_id": str(aid)})
    await PlatformDbApprovals(pool).verify(_check(aid, plan, key="first"))  # the same run (replay) is fine
    with pytest.raises(ApprovalRejected) as info:
        await PlatformDbApprovals(pool).verify(_check(aid, plan, key="second"))
    assert info.value.code == "APPROVAL_ALREADY_USED"


async def test_missing_approvals_table_is_refused_not_skipped(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    with pytest.raises(ApprovalRejected) as info:
        await PlatformDbApprovals(pool).verify(_check(uuid.uuid4(), uuid.uuid4()))
    assert info.value.code == "APPROVAL_SOURCE_UNAVAILABLE"
