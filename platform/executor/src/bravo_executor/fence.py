"""Resource-side fence (D-19).

Every Executor control transaction on src or dst starts with

    UPDATE bravo_fence SET epoch = $e WHERE id = 1 AND epoch <= $e

and is abandoned when no row was updated. Once a newer run has written a higher
epoch, a late command from an older Executor fails inside the database itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection

FENCE_SQL = "UPDATE bravo_fence SET epoch = %s WHERE id = 1 AND epoch <= %s"


class FenceRejected(RuntimeError):
    """The resource holds a higher epoch than this run. Nothing in the transaction applied."""

    def __init__(self, side: str, epoch: int) -> None:
        super().__init__(f"{side}: bravo_fence holds an epoch higher than {epoch}")
        self.side = side
        self.epoch = epoch


@asynccontextmanager
async def fenced(conn: AsyncConnection, epoch: int, side: str) -> AsyncIterator[AsyncConnection]:
    """Open a transaction that is valid only while ``epoch`` is the newest on the resource.

    Raises FenceRejected (the transaction is rolled back) when the fence row is not updated.
    """
    async with conn.transaction():
        cur = await conn.execute(FENCE_SQL, (epoch, epoch))
        if cur.rowcount != 1:
            raise FenceRejected(side, epoch)
        yield conn


async def read_fence(conn: AsyncConnection) -> int | None:
    cur = await conn.execute("SELECT epoch FROM bravo_fence WHERE id = 1")
    row = await cur.fetchone()
    return None if row is None else int(row[0])
