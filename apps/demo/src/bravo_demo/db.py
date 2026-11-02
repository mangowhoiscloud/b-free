"""Chat database access through the PgBouncer DSN (transaction pooling, §8.10).

* No server-side prepared statements (``prepare_threshold=None``): PgBouncer runs with
  ``max_prepared_statements = 0`` and moves transactions between server connections.
* No session state: every write sets ``statement_timeout`` with ``SET LOCAL`` (≤ 200 ms).
* No LISTEN/NOTIFY: fan-out polls ``messages`` (fanout.py).
* A write waits at most ``WRITE_WAIT_S`` (8 s) in total. During a PgBouncer PAUSE the query
  waits in PgBouncer's queue; after the deadline the caller gets ``WriteTimeout`` and the user
  retries with the same idempotency key, so a write that did land is not duplicated.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .config import DbSettings

log = logging.getLogger(__name__)
T = TypeVar("T")

# SQLSTATEs raised by the attachment guard triggers (schema/sql/01_schema.sql).
SQLSTATE_ATTACH_FROZEN = "ZB001"
SQLSTATE_ATTACH_STORE_MISMATCH = "ZB002"
SQLSTATE_ATTACH_IMMUTABLE = "ZB003"
SQLSTATE_ATTACH_STORE_MISSING = "ZB004"
# Retry inside the deadline: lost connection, admin shutdown (cutover step 4), query canceled.
_RETRY_CLASSES = ("08", "57")


class WriteTimeout(Exception):
    """The write did not finish within WRITE_WAIT_S. It may still land; retry with the same key."""


class IdempotencyConflict(Exception):
    """The idempotency key was already used with a different request."""


class AttachmentNotFound(Exception):
    """The attachment does not exist in this room, or it was deleted."""


class WritesBlocked(Exception):
    """The write-block switch of this environment is on (spec r2 §8.10). Retry later, same key."""

    def __init__(self, since: datetime | None) -> None:
        super().__init__("writes blocked")
        self.since = since


@dataclass(frozen=True, slots=True)
class WriteBlockState:
    blocked: bool
    since: datetime | None
    changed_by: str | None

    def public(self) -> dict[str, Any]:
        return {"blocked": self.blocked, "since": self.since.isoformat() if self.since else None}


class AttachmentsPaused(Exception):
    """The guard trigger rejected the row: store frozen or not the active store (D-04)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # frozen | store_mismatch | store_missing


@dataclass(frozen=True, slots=True)
class Attachment:
    id: uuid.UUID
    logical_id: uuid.UUID
    idem_key: uuid.UUID
    room: str
    author: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    filename: str
    store_id: str
    created_at: datetime
    deleted_at: datetime | None

    def public(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "logical_id": str(self.logical_id),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "filename": self.filename,
            "store_id": self.store_id,
            "created_at": self.created_at.isoformat(),
            "deleted": self.deleted_at is not None,
        }


_MESSAGE_COLUMNS = """
    m.id, m.room, m.author, m.body, m.idem_key, m.created_at,
    a.id AS att_id, a.logical_id AS att_logical_id, a.idem_key AS att_idem_key, a.room AS att_room,
    a.author AS att_author, a.object_key AS att_object_key, a.sha256 AS att_sha256,
    a.size_bytes AS att_size_bytes, a.content_type AS att_content_type, a.filename AS att_filename,
    a.store_id AS att_store_id, a.created_at AS att_created_at, a.deleted_at AS att_deleted_at
"""
_MESSAGE_FROM = """
    FROM messages m
    LEFT JOIN message_attachments ma ON ma.message_id = m.id
    LEFT JOIN attachments a ON a.id = ma.attachment_id
"""
# One write, one round trip (contract 15 §10, 2026-11-03): during SHIFT the destination app writes to the source
# primary over the D-16 link, where every statement paid one link round trip (measured 340 ms on-prem). The
# transaction BEGIN, set_config, guard, INSERT, SELECT, COMMIT cost 6 of them (2,040 ms). Sent as one simple-query
# message (client-side binding), the statements run in one implicit transaction and cost one round trip.
# The guard keeps its FOR SHARE (the ROW SHARE lock the block switch waits for); a blocked row fails the batch
# with division by zero, so the INSERT never runs, and the caller reads ``since`` afterwards.
_SEND_BATCH = (
    "SELECT set_config('statement_timeout', %s, true);"
    "SELECT since, 1 / CASE WHEN blocked THEN 0 ELSE 1 END AS open FROM write_block WHERE env_id = %s FOR SHARE;"
    "INSERT INTO messages (room, author, body, idem_key) VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (idem_key) DO NOTHING RETURNING id;"
    f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM} WHERE m.idem_key = %s"
)
_ATTACHMENT_COLUMNS = (
    "id, logical_id, idem_key, room, author, object_key, sha256, size_bytes, content_type, filename, "
    "store_id, created_at, deleted_at"
)


def _attachment_from(row: dict[str, Any], prefix: str = "") -> Attachment | None:
    if row.get(f"{prefix}id") is None:
        return None
    return Attachment(
        id=row[f"{prefix}id"],
        logical_id=row[f"{prefix}logical_id"],
        idem_key=row[f"{prefix}idem_key"],
        room=row[f"{prefix}room"],
        author=row[f"{prefix}author"],
        object_key=row[f"{prefix}object_key"],
        sha256=row[f"{prefix}sha256"],
        size_bytes=row[f"{prefix}size_bytes"],
        content_type=row[f"{prefix}content_type"],
        filename=row[f"{prefix}filename"],
        store_id=row[f"{prefix}store_id"],
        created_at=row[f"{prefix}created_at"],
        deleted_at=row[f"{prefix}deleted_at"],
    )


def message_public(row: dict[str, Any]) -> dict[str, Any]:
    att = _attachment_from(row, "att_")
    return {
        "id": row["id"],
        "room": row["room"],
        "author": row["author"],
        "body": row["body"],
        "idem_key": str(row["idem_key"]),
        "created_at": row["created_at"].isoformat(),
        "attachment": att.public() if att else None,
    }


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, PoolTimeout | psycopg.OperationalError | psycopg.InterfaceError):
        return True
    state = getattr(exc, "sqlstate", None)
    return bool(state) and state[:2] in _RETRY_CLASSES


class ChatDb:
    def __init__(self, settings: DbSettings, password: str, application_name: str, env_id: str) -> None:
        self.settings = settings
        self.env_id = env_id
        self._background: set[asyncio.Task[Any]] = set()
        self.pool = AsyncConnectionPool(
            conninfo=settings.dsn,
            kwargs={
                "password": password,
                "autocommit": True,
                "prepare_threshold": None,
                "application_name": application_name[:63],
                "connect_timeout": settings.connect_timeout_s,
            },
            min_size=settings.pool_min,
            max_size=settings.pool_max,
            open=False,
            name="chat",
            max_lifetime=600.0,
            max_idle=120.0,
            reconnect_timeout=300.0,
        )

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self.pool.close(timeout=2.0)

    # --- deadline handling -------------------------------------------------------------------

    async def _bounded(self, op: Callable[[], Awaitable[T]], deadline: float) -> T:
        """Run one attempt until ``deadline``. On timeout, do not wait for the cancelled attempt:
        its cleanup (ROLLBACK) may itself sit in PgBouncer's paused queue."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WriteTimeout
        task = asyncio.ensure_future(op())
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if task in done:
            return task.result()
        task.cancel()
        self._background.add(task)
        task.add_done_callback(self._reap)
        raise WriteTimeout

    def _reap(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("abandoned attempt ended with %s", type(task.exception()).__name__)

    async def with_deadline(self, op: Callable[[AsyncConnection], Awaitable[T]], wait_s: float | None = None) -> T:
        """Retry ``op`` on connection-level errors until the write deadline (default WRITE_WAIT_S)."""
        deadline = time.monotonic() + (self.settings.write_wait_s if wait_s is None else wait_s)
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WriteTimeout

            async def one_attempt() -> T:
                async with self.pool.connection(timeout=max(0.05, deadline - time.monotonic())) as conn:
                    return await op(conn)

            try:
                return await self._bounded(one_attempt, deadline)
            except WriteTimeout:
                raise
            except Exception as exc:
                if not _retryable(exc):
                    raise
                attempt += 1
                delay = min(0.1 * 2**attempt, 1.0, max(0.0, deadline - time.monotonic()))
                log.info("database attempt %d failed (%s); retrying in %.2fs", attempt, type(exc).__name__, delay)
                await asyncio.sleep(delay)

    async def _set_local_timeout(self, conn: AsyncConnection) -> None:
        await conn.execute(
            "SELECT set_config('statement_timeout', %s, true)", (f"{self.settings.statement_timeout_ms}ms",)
        )

    async def _guard_writes(self, conn: AsyncConnection) -> None:
        """First statement of every business write transaction. FOR SHARE takes a ROW SHARE lock
        on write_block (even when the row is missing), which conflicts with the switch's
        EXCLUSIVE lock: the switch waits for writes in flight, and later writes see its state."""
        cur = await conn.execute(
            "SELECT blocked, since FROM write_block WHERE env_id = %s FOR SHARE", (self.env_id,)
        )
        row = await cur.fetchone()
        if row is not None and row[0]:
            raise WritesBlocked(row[1])

    # --- write-block switch (admin port) --------------------------------------------------------

    async def ensure_write_block_row(self, changed_by: str) -> None:
        """Create this environment's row (unblocked) if missing, so `since` is always defined."""

        async def op(conn: AsyncConnection) -> None:
            await conn.execute(
                "INSERT INTO write_block (env_id, blocked, changed_by) VALUES (%s, false, %s) "
                "ON CONFLICT (env_id) DO NOTHING",
                (self.env_id, changed_by),
            )

        await self.with_deadline(op, 30.0)

    async def write_block_state(self, wait_s: float) -> WriteBlockState:
        async def op(conn: AsyncConnection) -> WriteBlockState:
            cur = await conn.execute(
                "SELECT blocked, since, changed_by FROM write_block WHERE env_id = %s", (self.env_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return WriteBlockState(False, None, None)
            return WriteBlockState(bool(row[0]), row[1], row[2])

        return await self.with_deadline(op, wait_s)

    async def set_write_block(self, blocked: bool, changed_by: str, wait_s: float) -> WriteBlockState:
        """Turn the switch on or off. Returns after every write that saw the old state has ended."""

        async def op(conn: AsyncConnection) -> WriteBlockState:
            async with conn.transaction():
                await conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(wait_s * 1000)}ms",))
                await conn.execute("LOCK TABLE write_block IN EXCLUSIVE MODE")
                cur = await conn.execute(
                    """
                    INSERT INTO write_block AS w (env_id, blocked, since, changed_by)
                    VALUES (%s, %s, now(), %s)
                    ON CONFLICT (env_id) DO UPDATE
                      SET blocked = EXCLUDED.blocked,
                          since = CASE WHEN w.blocked = EXCLUDED.blocked THEN w.since ELSE now() END,
                          changed_by = CASE WHEN w.blocked = EXCLUDED.blocked THEN w.changed_by
                                            ELSE EXCLUDED.changed_by END
                    RETURNING blocked, since, changed_by
                    """,
                    (self.env_id, blocked, changed_by),
                )
                row = await cur.fetchone()
            assert row is not None
            return WriteBlockState(bool(row[0]), row[1], row[2])

        return await self.with_deadline(op, wait_s)

    # --- messages ----------------------------------------------------------------------------

    async def send_message(
        self, *, room: str, author: str, body: str, idem_key: uuid.UUID, attachment_id: uuid.UUID | None
    ) -> tuple[dict[str, Any], bool]:
        """Insert once per idempotency key. Returns (message, created)."""

        async def one_round_trip(conn: AsyncConnection) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            cur = psycopg.AsyncClientCursor(conn, row_factory=dict_row)
            try:
                await cur.execute(_SEND_BATCH, (f"{self.settings.statement_timeout_ms}ms", self.env_id, room, author,
                                                body, idem_key, idem_key))
            except psycopg.errors.DivisionByZero:
                since = await (await conn.execute(
                    "SELECT since FROM write_block WHERE env_id = %s", (self.env_id,))).fetchone()
                raise WritesBlocked(since[0] if since else None) from None
            # result sets in order: set_config, guard, INSERT ... RETURNING id, the message row
            cur.nextset()
            cur.nextset()
            inserted = await cur.fetchone()
            cur.nextset()
            return inserted, await cur.fetchone()

        async def op(conn: AsyncConnection) -> tuple[dict[str, Any], bool]:
            if attachment_id is None:
                inserted, row = await one_round_trip(conn)
                if row is None:  # unreachable: the row was inserted or already existed
                    raise RuntimeError("message vanished after insert")
                if inserted is None and not (row["room"] == room and row["author"] == author
                                             and row["body"] == body and row["att_id"] is None):
                    raise IdempotencyConflict(str(idem_key))
                return message_public(row), inserted is not None
            async with conn.transaction():
                await self._set_local_timeout(conn)
                await self._guard_writes(conn)
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    """
                    INSERT INTO messages (room, author, body, idem_key) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (idem_key) DO NOTHING
                    RETURNING id
                    """,
                    (room, author, body, idem_key),
                )
                inserted = await cur.fetchone()
                if inserted is not None and attachment_id is not None:
                    await cur.execute(
                        """
                        INSERT INTO message_attachments (message_id, attachment_id)
                        SELECT %s, a.id FROM attachments a
                        WHERE a.id = %s AND a.room = %s AND a.deleted_at IS NULL
                        """,
                        (inserted["id"], attachment_id, room),
                    )
                    if cur.rowcount != 1:
                        raise AttachmentNotFound(str(attachment_id))
                await cur.execute(f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM} WHERE m.idem_key = %s", (idem_key,))
                row = await cur.fetchone()
            if row is None:  # unreachable: the row was inserted or already existed
                raise RuntimeError("message vanished after insert")
            if inserted is None:
                same = (
                    row["room"] == room
                    and row["author"] == author
                    and row["body"] == body
                    and (row["att_id"] == attachment_id)
                )
                if not same:
                    raise IdempotencyConflict(str(idem_key))
            return message_public(row), inserted is not None

        return await self.with_deadline(op)

    async def fetch_recent(self, room: str, limit: int) -> list[dict[str, Any]]:
        async def op(conn: AsyncConnection) -> list[dict[str, Any]]:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT * FROM (SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM} WHERE m.room = %s "
                f"ORDER BY m.id DESC LIMIT %s) t ORDER BY id",
                (room, limit),
            )
            return [message_public(r) for r in await cur.fetchall()]

        return await self.with_deadline(op)

    async def fetch_after(self, room: str, after_id: int, limit: int) -> list[dict[str, Any]]:
        async def op(conn: AsyncConnection) -> list[dict[str, Any]]:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM} WHERE m.room = %s AND m.id > %s ORDER BY m.id LIMIT %s",
                (room, after_id, limit),
            )
            return [message_public(r) for r in await cur.fetchall()]

        return await self.with_deadline(op)

    async def recent_ids(self, limit: int, wait_s: float) -> list[int]:
        async def op(conn: AsyncConnection) -> list[int]:
            cur = await conn.execute("SELECT id FROM messages ORDER BY id DESC LIMIT %s", (limit,))
            return sorted(int(r[0]) for r in await cur.fetchall())

        return await self.with_deadline(op, wait_s)

    async def poll(
        self, *, above: int, gaps: list[tuple[int, int]], limit: int, wait_s: float
    ) -> list[dict[str, Any]]:
        """Rows with id > ``above`` plus rows inside still-open gaps (late commits)."""
        lo = [g[0] for g in gaps]
        hi = [g[1] for g in gaps]

        async def op(conn: AsyncConnection) -> list[dict[str, Any]]:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""
                SELECT * FROM (
                  (SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM} WHERE m.id > %(above)s ORDER BY m.id LIMIT %(limit)s)
                  UNION ALL
                  (SELECT {_MESSAGE_COLUMNS}
                     FROM unnest(%(lo)s::bigint[], %(hi)s::bigint[]) AS g(lo, hi)
                     JOIN messages m ON m.id BETWEEN g.lo AND g.hi
                     LEFT JOIN message_attachments ma ON ma.message_id = m.id
                     LEFT JOIN attachments a ON a.id = ma.attachment_id)
                ) t ORDER BY id
                """,
                {"above": above, "limit": limit, "lo": lo, "hi": hi},
            )
            return await cur.fetchall()

        return await self.with_deadline(op, wait_s)

    # --- attachments -------------------------------------------------------------------------

    async def attachment_by_idem(self, idem_key: uuid.UUID) -> Attachment | None:
        async def op(conn: AsyncConnection) -> Attachment | None:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {_ATTACHMENT_COLUMNS} FROM attachments WHERE idem_key = %s", (idem_key,))
            row = await cur.fetchone()
            return _attachment_from(row) if row else None

        return await self.with_deadline(op)

    async def attachment(self, attachment_id: uuid.UUID) -> Attachment | None:
        async def op(conn: AsyncConnection) -> Attachment | None:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"SELECT {_ATTACHMENT_COLUMNS} FROM attachments WHERE id = %s", (attachment_id,))
            row = await cur.fetchone()
            return _attachment_from(row) if row else None

        return await self.with_deadline(op)

    async def insert_attachment(
        self,
        *,
        logical_id: uuid.UUID,
        idem_key: uuid.UUID,
        room: str,
        author: str,
        sha256: str,
        size_bytes: int,
        content_type: str,
        filename: str,
        store_id: str,
    ) -> tuple[Attachment, bool]:
        """Commit the attachment row after the object is stored and verified (D-04 order)."""

        async def op(conn: AsyncConnection) -> tuple[Attachment, bool]:
            try:
                async with conn.transaction():
                    await self._set_local_timeout(conn)
                    await self._guard_writes(conn)
                    cur = conn.cursor(row_factory=dict_row)
                    await cur.execute(
                        f"""
                        INSERT INTO attachments (logical_id, idem_key, room, author, object_key, sha256,
                                                 size_bytes, content_type, filename, store_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idem_key) DO NOTHING
                        RETURNING {_ATTACHMENT_COLUMNS}
                        """,
                        (logical_id, idem_key, room, author, f"att/{sha256}", sha256, size_bytes,
                         content_type, filename, store_id),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        await cur.execute(
                            f"SELECT {_ATTACHMENT_COLUMNS} FROM attachments WHERE idem_key = %s", (idem_key,)
                        )
                        row = await cur.fetchone()
                        if row is None:
                            raise RuntimeError("attachment vanished after conflict")
                        existing = _attachment_from(row)
                        assert existing is not None
                        if existing.sha256 != sha256 or existing.room != room:
                            raise IdempotencyConflict(str(idem_key))
                        return existing, False
            except psycopg.Error as exc:
                state = exc.sqlstate or ""
                if state == SQLSTATE_ATTACH_FROZEN:
                    raise AttachmentsPaused("frozen") from exc
                if state == SQLSTATE_ATTACH_STORE_MISMATCH:
                    raise AttachmentsPaused("store_mismatch") from exc
                if state == SQLSTATE_ATTACH_STORE_MISSING:
                    raise AttachmentsPaused("store_missing") from exc
                raise
            created = _attachment_from(row)
            assert created is not None
            return created, True

        return await self.with_deadline(op)
