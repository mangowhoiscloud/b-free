"""D-04 attachment freeze, store switch and ABORT restore on the real demo chat schema."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import psycopg
import pytest
from psycopg import AsyncConnection

from bravo_executor.chatdb import ChatDb

INSERT = """
INSERT INTO attachments (logical_id, idem_key, room, author, object_key, sha256, size_bytes, content_type,
                         filename, store_id)
VALUES (%s, %s, 'r', 'a', %s, %s, 3, 'text/plain', 'a.txt', %s)
"""


def _args(store: str = "src") -> tuple[object, ...]:
    digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return (uuid.uuid4(), uuid.uuid4(), f"att/{digest}", digest, store)


async def test_freeze_waits_for_inflight_insert_then_rejects_new_ones(chat_dsn: str) -> None:
    async with (
        await AsyncConnection.connect(chat_dsn) as writer,  # not autocommit: holds its insert open
        await AsyncConnection.connect(chat_dsn, autocommit=True) as ctl_conn,
        await AsyncConnection.connect(chat_dsn, autocommit=True) as late,
    ):
        ctl = ChatDb(ctl_conn, "src")
        await writer.execute(INSERT, _args())  # in flight, not committed
        freeze = asyncio.create_task(ctl.freeze_attachments(epoch=1, lock_timeout_ms=5000))
        await asyncio.sleep(0.3)
        assert not freeze.done(), "freeze must wait for the in-flight insert"
        await writer.commit()
        await asyncio.wait_for(freeze, 5)
        state = await ctl.attachment_store()
        assert state is not None and state["frozen"] is True and state["freeze_watermark"] is not None
        with pytest.raises(psycopg.Error) as info:
            await late.execute(INSERT, _args())
        assert info.value.sqlstate == "ZB001"
        cur = await ctl_conn.execute("SELECT count(*) FROM attachments")
        row = await cur.fetchone()
        assert row is not None and row[0] == 1  # the in-flight insert landed before the watermark


async def test_freeze_gives_up_on_lock_timeout_without_freezing(chat_dsn: str) -> None:
    async with (
        await AsyncConnection.connect(chat_dsn) as writer,
        await AsyncConnection.connect(chat_dsn, autocommit=True) as ctl_conn,
    ):
        ctl = ChatDb(ctl_conn, "src")
        await writer.execute(INSERT, _args())
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await ctl.freeze_attachments(epoch=1, lock_timeout_ms=200)
        await writer.rollback()
        state = await ctl.attachment_store()
        assert state is not None and state["frozen"] is False


async def test_switch_unfreezes_with_new_store_and_restore_puts_back_the_old_one(chat_dsn: str) -> None:
    async with await AsyncConnection.connect(chat_dsn, autocommit=True) as conn:
        ctl = ChatDb(conn, "src")
        prior = await ctl.attachment_store()
        assert prior is not None
        await ctl.freeze_attachments(epoch=1, lock_timeout_ms=1000)
        await ctl.restore_attachment_store(1, prior)  # ABORT_PRE_WRITE path
        assert await ctl.attachment_store() == prior
        await conn.execute(INSERT, _args("src"))  # writes work again on the old store

        await ctl.freeze_attachments(epoch=2, lock_timeout_ms=1000)
        await ctl.switch_attachment_store(2, "dst")  # step 8 path
        now = await ctl.attachment_store()
        assert now is not None
        assert (now["active_store"], now["frozen"], now["generation"]) == ("dst", False, prior["generation"] + 1)
        with pytest.raises(psycopg.Error) as info:
            await conn.execute(INSERT, _args("src"))  # the old store is no longer accepted
        assert info.value.sqlstate == "ZB002"
        await conn.execute(INSERT, _args("dst"))


async def test_uuid_and_bigint_pk_tables_digest_on_real_rows(chat_dsn: str) -> None:
    async with await AsyncConnection.connect(chat_dsn, autocommit=True) as conn:
        for _ in range(20):
            await conn.execute(INSERT, _args())
        db = ChatDb(conn, "src")
        d1 = await db.table_digest("attachments")
        d2 = await db.table_digest("attachments")
        assert d1.rows == 20 and d1.as_dict() == d2.as_dict()
        empty = await db.table_digest("message_attachments")
        assert empty.rows == 0
