"""bravo_fence epoch check on a real PostgreSQL (E5: a late command with an old epoch fails)."""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from bravo_executor.fence import FenceRejected, fenced, read_fence


async def _probe_count(conn: AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) FROM fenced_probe")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_newer_or_equal_epoch_is_accepted(schema_dsn: str) -> None:
    async with await AsyncConnection.connect(schema_dsn, autocommit=True) as conn:
        async with fenced(conn, 5, "src"):
            await conn.execute("INSERT INTO fenced_probe VALUES (1)")
        async with fenced(conn, 5, "src"):  # same epoch: the same run may issue more commands
            await conn.execute("INSERT INTO fenced_probe VALUES (2)")
        assert await read_fence(conn) == 5
        assert await _probe_count(conn) == 2


async def test_older_epoch_is_rejected_and_its_work_rolled_back(schema_dsn: str) -> None:
    async with await AsyncConnection.connect(schema_dsn, autocommit=True) as conn:
        async with fenced(conn, 7, "dst"):
            pass
        with pytest.raises(FenceRejected) as info:
            async with fenced(conn, 6, "dst"):
                await conn.execute("INSERT INTO fenced_probe VALUES (99)")
        assert info.value.side == "dst" and info.value.epoch == 6
        assert await read_fence(conn) == 7
        assert await _probe_count(conn) == 0


async def test_late_command_from_old_executor_fails_after_new_epoch(schema_dsn: str) -> None:
    """Two sessions: the old Executor (epoch 1) opens its transaction after the new one wrote epoch 2."""
    async with (
        await AsyncConnection.connect(schema_dsn, autocommit=True) as old,
        await AsyncConnection.connect(schema_dsn, autocommit=True) as new,
    ):
        async with fenced(old, 1, "src"):
            pass
        async with fenced(new, 2, "src"):
            await new.execute("INSERT INTO fenced_probe VALUES (2)")
        with pytest.raises(FenceRejected):
            async with fenced(old, 1, "src"):
                await old.execute("INSERT INTO fenced_probe VALUES (1)")
        assert await _probe_count(new) == 1
