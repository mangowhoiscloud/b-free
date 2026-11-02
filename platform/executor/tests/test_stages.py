"""Stage bookkeeping and effect-ledger transitions on the real contract tables."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.fence import fenced
from bravo_executor.ledger import EffectState, PostgresLedger, RunRequest
from bravo_executor.stages import EffectRunner, EffectUnconfirmed, StageRecorder, StepFailed


class StepClock:
    """Deterministic monotonic clock: each call advances by ``step_ns``."""

    def __init__(self, step_ns: int) -> None:
        self.now = 0
        self.step_ns = step_ns

    def __call__(self) -> int:
        self.now += self.step_ns
        return self.now


async def _run(ledger: PostgresLedger) -> uuid.UUID:
    run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=uuid.uuid4(), epoch=1, idem_key=str(uuid.uuid4())))
    return run_id


async def _rows(pool: AsyncConnectionPool[AsyncConnection], query: str, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, (run_id,))
        return list(await cur.fetchall())


async def test_stage_rows_have_sequence_ms_and_result(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    run_id = await _run(ledger)
    rec = StageRecorder(ledger, run_id, clock_ns=StepClock(7_000_000), wall=lambda: datetime(2026, 11, 2, tzinfo=UTC))
    async with rec.stage("1_preflight") as st:
        st.detail["lag_bytes"] = 0
    with pytest.raises(StepFailed):
        async with rec.stage("2_3_pause"):
            raise StepFailed("LONG_TRANSACTION", {"count": 1})
    rows = await _rows(
        pool, "SELECT seq, name, ms, result, detail FROM run_stages WHERE run_id = %s ORDER BY seq", run_id
    )
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [(1, "1_preflight", 7, "OK"), (2, "2_3_pause", 7, "FAILED")]
    assert rows[0][4] == {"lag_bytes": 0}
    assert rows[1][4]["reason"] == "LONG_TRANSACTION" and rows[1][4]["reason_detail"] == {"count": 1}
    assert rec.timings_ms == {"1_preflight": 7, "2_3_pause": 7}


async def _effects(pool: AsyncConnectionPool[AsyncConnection], run_id: uuid.UUID) -> dict[str, tuple[str, Any]]:
    rows = await _rows(pool, "SELECT action, state, evidence FROM effects WHERE run_id = %s", run_id)
    return {r[0]: (r[1], r[2]) for r in rows}


async def test_effect_confirmed_applied_after_observation(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    run_id = await _run(ledger)
    fx = EffectRunner(ledger, run_id, epoch=1)
    seen: list[str] = []

    async def call() -> dict[str, Any]:
        # The ledger must already show IN_FLIGHT when the external call is made.
        seen.append((await _effects(pool, run_id))["PAUSE"][0])
        return {"sent": True}

    async def observe() -> tuple[bool, dict[str, Any]]:
        return True, {"paused": 1}

    await fx.apply("pgbouncer/0", "PAUSE", call, observe)
    assert seen == [EffectState.IN_FLIGHT]
    state, evidence = (await _effects(pool, run_id))["PAUSE"]
    assert state == EffectState.CONFIRMED_APPLIED
    assert evidence == {"call": {"sent": True}, "observed": {"paused": 1}}


async def test_effect_unknown_when_observation_disagrees(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    run_id = await _run(ledger)
    fx = EffectRunner(ledger, run_id, epoch=1)

    async def call() -> None:
        return None

    async def observe() -> tuple[bool, dict[str, Any]]:
        return False, {"paused": 0}

    with pytest.raises(EffectUnconfirmed):
        await fx.apply("pgbouncer/0", "PAUSE", call, observe)
    assert (await _effects(pool, run_id))["PAUSE"][0] == EffectState.UNKNOWN


async def test_effect_unknown_when_call_fails(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    run_id = await _run(ledger)
    fx = EffectRunner(ledger, run_id, epoch=1)

    async def call() -> None:
        raise ConnectionResetError("response lost")

    async def observe() -> tuple[bool, dict[str, Any]]:  # pragma: no cover - not reached
        return True, {}

    with pytest.raises(ConnectionResetError):
        await fx.apply("pgbouncer/0", "RESUME", call, observe)
    state, evidence = (await _effects(pool, run_id))["RESUME"]
    assert state == EffectState.UNKNOWN and evidence["reason"] == "CALL_FAILED"


async def test_effect_not_applied_only_on_fence_rejection(
    pool: AsyncConnectionPool[AsyncConnection], schema_dsn: str
) -> None:
    ledger = PostgresLedger(pool)
    run_id = await _run(ledger)
    fx = EffectRunner(ledger, run_id, epoch=1)
    async with await AsyncConnection.connect(schema_dsn, autocommit=True) as conn:
        async with fenced(conn, 9, "src"):  # a newer run already holds epoch 9
            pass

        async def call() -> None:
            async with fenced(conn, 1, "src"):
                await conn.execute("INSERT INTO fenced_probe VALUES (1)")

        async def observe() -> tuple[bool, dict[str, Any]]:  # pragma: no cover - not reached
            return True, {}

        with pytest.raises(StepFailed) as info:
            await fx.apply("src/db/role:app_writer", "NOLOGIN", call, observe)
        assert info.value.code == "FENCE_REJECTED"
    state, evidence = (await _effects(pool, run_id))["NOLOGIN"]
    assert state == EffectState.CONFIRMED_NOT_APPLIED and evidence["reason"] == "FENCE_REJECTED"
