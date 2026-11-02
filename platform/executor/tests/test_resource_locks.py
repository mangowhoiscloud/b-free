"""resource_locks (D-19): all-or-nothing reservation (E2) and no release on lease expiry (E3)."""

from __future__ import annotations

import uuid

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.config import Resources, Settings
from bravo_executor.ledger import EffectState, PostgresLedger, ResourceBusy, RunRequest


async def _run(ledger: PostgresLedger, key: str) -> uuid.UUID:
    run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=uuid.uuid4(), epoch=1, idem_key=key))
    return run_id


async def _held(pool: AsyncConnectionPool[AsyncConnection]) -> dict[str, uuid.UUID]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT resource, run_id FROM resource_locks")
        return {r[0]: r[1] for r in await cur.fetchall()}


async def test_e2_conflicting_run_is_refused_and_takes_nothing(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    a, b = await _run(ledger, "a"), await _run(ledger, "b")
    await ledger.reserve_locks(a, ["pgbouncer/0", "src/db/role:app_writer"], "exec-1", 1, 60)
    with pytest.raises(ResourceBusy) as info:
        await ledger.reserve_locks(b, ["dst/db/role:app_writer", "pgbouncer/0"], "exec-2", 2, 60)
    assert info.value.held == {"pgbouncer/0": str(a)}
    held = await _held(pool)
    assert set(held) == {"pgbouncer/0", "src/db/role:app_writer"}  # b reserved nothing, not even dst
    assert set(held.values()) == {a}


async def test_e3_lease_expiry_alone_does_not_release(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    a, b = await _run(ledger, "a"), await _run(ledger, "b")
    await ledger.reserve_locks(a, ["pgbouncer/0"], "exec-1", 1, 0.001)
    effect = await ledger.effect_prepare(a, "pgbouncer/0", "PAUSE", 1)
    await ledger.effect_transition(effect, EffectState.PREPARED, EffectState.IN_FLIGHT)
    await ledger.effect_transition(effect, EffectState.IN_FLIGHT, EffectState.UNKNOWN, {"reason": "lease expired"})
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT bool_and(lease_expires_at < now()) FROM resource_locks")
        row = await cur.fetchone()
        assert row is not None and row[0] is True  # the lease has expired
    with pytest.raises(ResourceBusy):
        await ledger.reserve_locks(b, ["pgbouncer/0"], "exec-2", 2, 60)
    assert await ledger.release_locks_if_resolved(a) is False  # UNKNOWN effect keeps the lock
    data = await ledger.get_run(a)
    assert data is not None and data["effects"][0]["state"] == EffectState.UNKNOWN


async def test_locks_released_only_when_every_effect_is_confirmed(pool: AsyncConnectionPool[AsyncConnection]) -> None:
    ledger = PostgresLedger(pool)
    a, b = await _run(ledger, "a"), await _run(ledger, "b")
    await ledger.reserve_locks(a, ["pgbouncer/0"], "exec-1", 1, 60)
    effect = await ledger.effect_prepare(a, "pgbouncer/0", "PAUSE", 1)
    await ledger.effect_transition(effect, EffectState.PREPARED, EffectState.IN_FLIGHT)
    assert await ledger.release_locks_if_resolved(a) is False
    await ledger.effect_transition(effect, EffectState.IN_FLIGHT, EffectState.CONFIRMED_APPLIED, {"paused": 1})
    assert await ledger.release_locks_if_resolved(a) is True
    await ledger.reserve_locks(b, ["pgbouncer/0"], "exec-2", 2, 60)
    assert set((await _held(pool)).values()) == {b}


def test_cutover_and_failback_share_one_lock_set() -> None:
    env = {k: "x" for k in (
        "PLATFORM_DB_DSN", "BRAVO_SRC_DSN", "BRAVO_DST_DSN", "BRAVO_SRC_APP_DSN", "BRAVO_DST_APP_DSN",
        "BRAVO_SRC_OBSERVE_DSN", "BRAVO_DST_OBSERVE_DSN", "BRAVO_PGB_STATS_DSN", "BRAVO_PGB_APP_DSN",
        "BRAVO_PGB_ADMIN_DSN", "BRAVO_PGB_INI_PATH", "BRAVO_PGB_DB_NAME", "BRAVO_CHAT_DB",
        "BRAVO_PGB_SRC_TARGET", "BRAVO_PGB_DST_TARGET", "BRAVO_SRC_STORE_ID", "BRAVO_DST_STORE_ID",
    )}
    locks = Resources.lock_set(Settings.from_env(env))
    for rid in ("pgbouncer/0", "src/db/role:app_writer", "dst/db/role:app_writer", "src/db/slot:bravo_fwd_sub",
                "dst/db/slot:bravo_rev_sub", "src/db/grant:messages:app_writer", "src/db/attachment_store",
                "dst/db/attachment_store"):
        assert rid in locks
    assert locks == sorted(set(locks))
