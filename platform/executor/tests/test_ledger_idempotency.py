"""Idempotency of run admission on the real contract tables (CONTRACT.md 5, 6)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.ledger import IdempotencyConflict, LedgerStateError, PostgresLedger, RunRequest, RunStatus


async def test_same_key_returns_same_run_id(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    req = RunRequest(plan_id=uuid.uuid4(), epoch=3, idem_key="k-1")
    run_id, created = await ledger.create_or_get_run(req)
    again, created_again = await ledger.create_or_get_run(req)
    assert created is True and created_again is False
    assert again == run_id
    assert await ledger.find_run_by_key("k-1") == run_id
    assert await ledger.running_run_ids() == [run_id]


@pytest.mark.parametrize("field", ["plan_id", "epoch", "kind"])
async def test_same_key_different_input_is_a_conflict(pool: AsyncConnectionPool[AsyncConnection], field: str) -> None:
    ledger = PostgresLedger(pool)
    base = RunRequest(plan_id=uuid.uuid4(), epoch=3, idem_key="k-2")
    run_id, _ = await ledger.create_or_get_run(base)
    changed = {
        "plan_id": RunRequest(plan_id=uuid.uuid4(), epoch=3, idem_key="k-2"),
        "epoch": RunRequest(plan_id=base.plan_id, epoch=4, idem_key="k-2"),
        "kind": RunRequest(plan_id=base.plan_id, epoch=3, idem_key="k-2", kind="failback"),
    }[field]
    with pytest.raises(IdempotencyConflict) as info:
        await ledger.create_or_get_run(changed)
    assert info.value.run_id == run_id


async def test_concurrent_requests_with_one_key_create_one_run(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    req = RunRequest(plan_id=uuid.uuid4(), epoch=1, idem_key="k-race")
    results = await asyncio.gather(*(ledger.create_or_get_run(req) for _ in range(8)))
    assert len({run_id for run_id, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_finish_run_only_from_running(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=uuid.uuid4(), epoch=1, idem_key="k-3"))
    await ledger.finish_run(run_id, RunStatus.DONE, 101)
    data = await ledger.get_run(run_id)
    assert data is not None
    assert data["run"]["status"] == RunStatus.DONE and data["run"]["write_pause_ms"] == 101
    with pytest.raises(LedgerStateError):
        await ledger.finish_run(run_id, RunStatus.SAFE_HOLD, None)
