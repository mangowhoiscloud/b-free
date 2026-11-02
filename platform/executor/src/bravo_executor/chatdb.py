"""Operations on the chat databases (src and dst) as ``executor_ctl``.

Reads run outside the fence. Every change runs in a fenced transaction (fence.py).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from .fence import fenced
from .hashing import INT_PK, TEXT_PK, TableDigest, sequence_last_issued

# Hash rule v1.1 (hashing.py): published tables, their PK kind and the column order. The first
# column is the PK. attachment_store and bravo_fence are per-database and never compared.
DIGEST_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "messages": (INT_PK, ("id", "room", "author", "body", "idem_key", "created_at")),
    "bravo_barrier": (INT_PK, ("id", "plan_id", "epoch", "at")),
    "attachments": (TEXT_PK, (
        "id", "logical_id", "idem_key", "room", "author", "object_key", "sha256", "size_bytes",
        "content_type", "filename", "store_id", "created_at", "deleted_at",
    )),
    "message_attachments": (INT_PK, ("message_id", "attachment_id")),
}
SEQUENCES: tuple[str, ...] = ("messages_id_seq", "bravo_barrier_id_seq")
SCHEMA_TABLES: tuple[str, ...] = (
    "messages", "bravo_barrier", "bravo_fence", "attachments", "message_attachments", "attachment_store",
)


# Session settings the receipt hash and psycopg's type parsing depend on (audit: a role-level
# DateStyle of 'SQL, DMY' on a managed DB stopped the digest). Set on every control/observe session
# after connect, so they hold whatever the server, database or role defaults are.
PINNED_GUCS: tuple[tuple[str, str], ...] = (
    ("DateStyle", "ISO, MDY"),
    ("IntervalStyle", "postgres"),
    ("TimeZone", "UTC"),
    ("extra_float_digits", "3"),
    ("bytea_output", "hex"),
    ("client_encoding", "UTF8"),
    ("standard_conforming_strings", "on"),
)


async def pin_session(conn: AsyncConnection) -> None:
    for name, value in PINNED_GUCS:
        await conn.execute("SELECT set_config(%s, %s, false)", (name, value))


async def connect(dsn: str, timeout_s: float = 5.0) -> AsyncConnection:
    conn = await AsyncConnection.connect(
        dsn, autocommit=True, connect_timeout=max(1, int(timeout_s)), application_name="bravo-executor",
        prepare_threshold=None,
    )
    await pin_session(conn)
    return conn


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    exists: bool
    enabled: bool
    worker_pid: int | None
    received_lsn: str | None
    latest_end_lsn: str | None

    @property
    def streaming(self) -> bool:
        return self.exists and self.enabled and self.worker_pid is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "enabled": self.enabled,
            "worker_pid": self.worker_pid,
            "received_lsn": self.received_lsn,
            "latest_end_lsn": self.latest_end_lsn,
        }


class ChatDb:
    def __init__(self, conn: AsyncConnection, side: str) -> None:
        self.conn = conn
        self.side = side

    async def close(self) -> None:
        await self.conn.close()

    async def _one(self, query: Any, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        cur = await self.conn.execute(query, params)
        return await cur.fetchone()

    # ---- reads ---------------------------------------------------------------------------

    async def cluster_name(self) -> str:
        row = await self._one("SELECT current_setting('cluster_name')")
        return str(row[0]) if row else ""

    async def current_wal_lsn(self) -> str:
        row = await self._one("SELECT pg_current_wal_lsn()::text")
        return str(row[0]) if row else ""

    async def slot_lag_bytes(self, slot: str) -> int | None:
        row = await self._one(
            """
            SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint
            FROM pg_replication_slots WHERE slot_name = %s
            """,
            (slot,),
        )
        return None if row is None or row[0] is None else int(row[0])

    async def subscription(self, name: str) -> SubscriptionState:
        row = await self._one(
            """
            SELECT s.subenabled, st.pid, st.received_lsn::text, st.latest_end_lsn::text
            FROM pg_subscription s
            LEFT JOIN pg_stat_subscription st ON st.subid = s.oid AND st.relid IS NULL
            WHERE s.subname = %s AND s.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            """,
            (name,),
        )
        if row is None:
            return SubscriptionState(False, False, None, None, None)
        return SubscriptionState(True, bool(row[0]), row[1], row[2], row[3])

    async def role_can_login(self, role: str) -> bool | None:
        row = await self._one("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (role,))
        return None if row is None else bool(row[0])

    async def role_session_count(self, role: str) -> int:
        row = await self._one(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = %s AND pid <> pg_backend_pid()", (role,)
        )
        return int(row[0]) if row else 0

    async def long_transactions(self, role: str, max_s: float) -> int:
        row = await self._one(
            """
            SELECT count(*) FROM pg_stat_activity
            WHERE usename = %s AND xact_start IS NOT NULL
              AND now() - xact_start > make_interval(secs => %s)
            """,
            (role, max_s),
        )
        return int(row[0]) if row else 0

    async def writer_state(self, role: str) -> dict[str, bool | None]:
        """LOGIN attribute and INSERT privilege on messages: both must be off to fence a writer."""
        row = await self._one(
            """
            SELECT r.rolcanlogin, has_table_privilege(r.rolname, 'messages', 'INSERT')
            FROM pg_roles r WHERE r.rolname = %s
            """,
            (role,),
        )
        if row is None:
            return {"login": None, "insert": None}
        return {"login": bool(row[0]), "insert": bool(row[1])}

    async def subscription_slot_name(self, name: str) -> str | None:
        row = await self._one("SELECT subslotname FROM pg_subscription WHERE subname = %s", (name,))
        return None if row is None or row[0] is None else str(row[0])

    async def attachment_store(self) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT active_store, generation, frozen, freeze_watermark FROM attachment_store WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "active_store": row[0],
            "generation": int(row[1]),
            "frozen": bool(row[2]),
            "freeze_watermark": None if row[3] is None else row[3].isoformat(),
        }

    async def slot(self, name: str) -> dict[str, Any] | None:
        row = await self._one(
            "SELECT active, active_pid FROM pg_replication_slots WHERE slot_name = %s", (name,)
        )
        return None if row is None else {"active": bool(row[0]), "active_pid": row[1]}

    async def barrier_present(self, barrier_id: int) -> bool:
        row = await self._one("SELECT 1 FROM bravo_barrier WHERE id = %s", (barrier_id,))
        return row is not None

    async def sequence_last(self, seq: str) -> int:
        row = await self._one(
            sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(seq))
        )
        if row is None:  # pragma: no cover - a sequence always has one row
            raise RuntimeError(f"{self.side}: sequence {seq} returned no row")
        return sequence_last_issued(int(row[0]), bool(row[1]))

    async def row_count(self, table: str) -> int:
        row = await self._one(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        return int(row[0]) if row else 0

    async def table_digest(self, table: str) -> TableDigest:
        """Single pass, one snapshot: row count, PK hash and content hash (hash rule v1)."""
        pk_kind, columns = DIGEST_TABLES[table]
        order = (
            sql.Identifier(columns[0])
            if pk_kind == INT_PK
            else sql.SQL('{}::text COLLATE "C"').format(sql.Identifier(columns[0]))
        )
        query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.Identifier(table), order
        )
        digest = TableDigest(table, pk_kind)
        async with self.conn.transaction():
            async with self.conn.cursor(name=f"bravo_digest_{table}") as cur:
                cur.itersize = 5000
                await cur.execute(query)
                async for row in cur:
                    digest.add(row)
        return digest

    async def schema_hash(self) -> str:
        import hashlib

        h = hashlib.sha256()
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT table_name, ordinal_position, column_name, data_type, is_nullable,
                       coalesce(column_default, '')
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                ORDER BY table_name, ordinal_position
                """,
                (list(SCHEMA_TABLES),),
            )
            for row in await cur.fetchall():
                h.update(("col|" + "|".join(str(v) for v in row) + "\n").encode())
            await cur.execute(
                """
                SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = ANY(%s)
                ORDER BY tablename, indexname
                """,
                (list(SCHEMA_TABLES),),
            )
            for row in await cur.fetchall():
                h.update(("idx|" + "|".join(str(v) for v in row) + "\n").encode())
        return h.hexdigest()

    async def settings(self, names: tuple[str, ...]) -> dict[str, str]:
        async with self.conn.cursor() as cur:
            await cur.execute("SELECT name, setting FROM pg_settings WHERE name = ANY(%s)", (list(names),))
            return {str(r[0]): str(r[1]) for r in await cur.fetchall()}

    async def hba_rules(self) -> list[dict[str, Any]]:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT line_number, type, database, user_name, auth_method, error FROM pg_hba_file_rules"
            )
            return list(await cur.fetchall())

    async def role_has_password(self, role: str) -> bool | None:
        row = await self._one("SELECT bravo_catalog.role_has_password(%s)", (role,))
        return None if row is None or row[0] is None else bool(row[0])

    async def public_can_select(self, table: str) -> bool:
        row = await self._one("SELECT has_table_privilege('public', %s, 'SELECT')", (table,))
        return bool(row[0]) if row else False

    # ---- fenced changes ------------------------------------------------------------------

    async def touch_fence(self, epoch: int) -> None:
        async with fenced(self.conn, epoch, self.side):
            pass

    async def set_role_login(self, epoch: int, role: str, login: bool) -> None:
        stmt = sql.SQL("ALTER ROLE {} {}").format(sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN"))
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(stmt)

    async def set_insert_privilege(self, epoch: int, role: str, granted: bool) -> None:
        """GRANT/REVOKE INSERT on messages as the table owner (SET LOCAL ROLE, reverts at commit)."""
        verb = "GRANT INSERT ON messages TO {}" if granted else "REVOKE INSERT ON messages FROM {}"
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute("SET LOCAL ROLE chat_owner")
            await self.conn.execute(sql.SQL(verb).format(sql.Identifier(role)))

    async def freeze_attachments(self, epoch: int, lock_timeout_ms: int) -> None:
        """D-04 freeze: wait out in-flight attachment inserts, then freeze with a watermark.

        LOCK ... SHARE ROW EXCLUSIVE conflicts with the ROW EXCLUSIVE lock every INSERT holds, so
        the lock is granted only after in-flight inserts end, and inserts that start later wait
        for this commit and then see frozen = true (the guard trigger rejects them, ZB001).
        Runs as the table owner (LOCK in this mode needs more than SELECT).
        """
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(f"{lock_timeout_ms}ms")))
            await self.conn.execute("SET LOCAL ROLE chat_owner")
            await self.conn.execute("LOCK TABLE attachments IN SHARE ROW EXCLUSIVE MODE")
            await self.conn.execute(
                """
                UPDATE attachment_store
                   SET frozen = true, freeze_watermark = coalesce(freeze_watermark, clock_timestamp())
                 WHERE id = 1
                """
            )

    async def switch_attachment_store(self, epoch: int, active_store: str) -> None:
        """D-04 step 8: new active store and unfreeze in one transaction (generation + 1)."""
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(
                """
                UPDATE attachment_store
                   SET active_store = %s, frozen = false, freeze_watermark = NULL, generation = generation + 1
                 WHERE id = 1
                """,
                (active_store,),
            )

    async def restore_attachment_store(self, epoch: int, prior: dict[str, Any]) -> None:
        """ABORT_PRE_WRITE: put back the store, freeze flag and watermark this run found."""
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(
                """
                UPDATE attachment_store
                   SET active_store = %s, frozen = %s, freeze_watermark = %s::timestamptz
                 WHERE id = 1
                """,
                (prior["active_store"], prior["frozen"], prior["freeze_watermark"]),
            )

    async def detach_subscription_slot(self, epoch: int, name: str) -> None:
        """ALTER SUBSCRIPTION ... SET (slot_name = NONE): the subscription forgets its remote slot."""
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(
                sql.SQL("ALTER SUBSCRIPTION {} SET (slot_name = NONE)").format(sql.Identifier(name))
            )

    async def drop_slot(self, epoch: int, name: str) -> None:
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute("SELECT pg_drop_replication_slot(%s)", (name,))

    async def terminate_role_sessions(self, epoch: int, role: str, wait_ms: int = 2000) -> int:
        async with fenced(self.conn, epoch, self.side):
            cur = await self.conn.execute(
                """
                SELECT count(*) FILTER (WHERE pg_terminate_backend(pid, %s))
                FROM pg_stat_activity WHERE usename = %s AND pid <> pg_backend_pid()
                """,
                (wait_ms, role),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def insert_barrier(self, epoch: int, plan_id: str) -> tuple[int, str]:
        async with fenced(self.conn, epoch, self.side):
            cur = await self.conn.execute(
                "INSERT INTO bravo_barrier (plan_id, epoch) VALUES (%s, %s) RETURNING id, at",
                (plan_id, epoch),
            )
            row = await cur.fetchone()
        if row is None:  # pragma: no cover
            raise RuntimeError("barrier insert returned no row")
        return int(row[0]), row[1].isoformat()

    async def raise_sequence(self, epoch: int, seq: str, value: int) -> None:
        """setval only upward: GREATEST keeps a higher current value (rule: raise only)."""
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(
                sql.SQL(
                    "SELECT setval(%s::regclass, GREATEST(%s::bigint, (SELECT CASE WHEN is_called "
                    "THEN last_value ELSE last_value - 1 END FROM {})), true)"
                ).format(sql.Identifier(seq)),
                (seq, value),
            )

    async def set_subscription_enabled(self, epoch: int, name: str, enabled: bool) -> None:
        stmt = sql.SQL("ALTER SUBSCRIPTION {} {}").format(
            sql.Identifier(name), sql.SQL("ENABLE" if enabled else "DISABLE")
        )
        async with fenced(self.conn, epoch, self.side):
            await self.conn.execute(stmt)


async def wait_until(
    predicate: Any, timeout_s: float, interval_s: float = 0.02
) -> tuple[bool, float]:
    """Poll ``await predicate()`` until true or timeout. Returns (ok, waited_ms)."""
    start = time.monotonic()
    deadline = start + timeout_s
    while True:
        if await predicate():
            return True, (time.monotonic() - start) * 1000
        if time.monotonic() >= deadline:
            return False, (time.monotonic() - start) * 1000
        await asyncio.sleep(interval_s)
