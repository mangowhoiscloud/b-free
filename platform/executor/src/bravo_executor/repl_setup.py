"""Replication setup between two clusters over the link (CLOUD-E2E-CONTRACT.md 7, 03-migration.md 4.1).

Runs as ``executor_ctl`` (pg_create_subscription, REPLICATION, SET ROLE chat_owner), never as a
superuser, with the topology and OpenBao secrets the Executor uses (topology.py). Every action
prints one JSON line; nothing here prints a password.

  python -m bravo_executor.repl_setup status
  python -m bravo_executor.repl_setup setup      # forward bravo_fwd_sub (dst <- src, initial copy)
                                                 # and the prepared reverse bravo_rev_sub on src
  python -m bravo_executor.repl_setup rearm      # after a cutover + failback: back to the
                                                 # pre-cutover state, then setup again (T25 runs)

Topology keys used (besides those in topology.py):
  "replication": {"fwd_conninfo": "host=host.lima.internal port=15432",   # as seen from dst
                  "rev_conninfo": "host=dst-db-link.chat.svc port=5432",  # as seen from src
                  "label": "임시 링크(D-16 전)"}
Replicator passwords: field ``replicator`` of each side's chat-db secret.

The reverse subscription is created with ``connect = true`` so PostgreSQL records its tables; if
the reverse link is not up, setup stops and says so (no connect = false shortcut that would
leave the subscription without tables).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from .chatdb import ChatDb, connect
from .vault import SecretBroker

PUBLISHED = ("message_attachments", "attachments", "messages", "bravo_barrier")


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}, ensure_ascii=False))
    sys.stdout.flush()


class ReplSetup:
    def __init__(self, topo: dict[str, Any], broker: SecretBroker) -> None:
        self.topo = topo
        self.broker = broker
        self.repl = topo["replication"]
        self.label = self.repl.get("label") or topo.get("label")

    async def _db(self, side: str) -> ChatDb:
        d = self.topo["db"][side]
        dsn = make_conninfo(d["conninfo"], dbname=self.topo["chat_db"], user="executor_ctl",
                            password=await self.broker.field(d["secret"], "executor_ctl"),
                            application_name="bravo-repl-setup")
        return ChatDb(await connect(dsn, timeout_s=10), side)

    async def _conninfo(self, key: str, publisher_side: str) -> str:
        secret = self.topo["db"][publisher_side]["secret"]
        return make_conninfo(self.repl[key], dbname=self.topo["chat_db"], user="replicator",
                             password=await self.broker.field(secret, "replicator"))

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label}
        for side in ("src", "dst"):
            db = await self._db(side)
            try:
                subs = {}
                for name in ("bravo_fwd_sub", "bravo_rev_sub"):
                    st = await db.subscription(name)
                    if st.exists:
                        subs[name] = {**st.as_dict(), "slot": await db.subscription_slot_name(name)}
                slots = {}
                for slot in ("bravo_fwd_sub", "bravo_rev_sub"):
                    info = await db.slot(slot)
                    if info is not None:
                        slots[slot] = {**info, "lag_bytes": await db.slot_lag_bytes(slot)}
                out[side] = {"subscriptions": subs, "slots": slots, "writer": await db.writer_state("app_writer"),
                             "rows": await db.row_count("messages"), "attachment_store": await db.attachment_store()}
            finally:
                await db.close()
        return out

    @staticmethod
    async def _quiet(db: ChatDb) -> None:
        """CREATE SUBSCRIPTION carries the replicator password in its connection string. Before sending
        it, this session must not log statement text on error or otherwise (audit: a failed statement
        leaked a password into the RDS log). Needs GRANT SET ON PARAMETER ... TO executor_ctl
        (02_roles.sql, deploy/cloud/db_roles.py). If the server refuses, nothing is sent."""
        await db.conn.execute("SET log_min_error_statement = panic")
        await db.conn.execute("SET log_statement = 'none'")

    @staticmethod
    async def _loud(db: ChatDb) -> None:
        await db.conn.execute("RESET log_min_error_statement")
        await db.conn.execute("RESET log_statement")

    async def setup(self, sync_timeout_s: float) -> None:
        src, dst = await self._db("src"), await self._db("dst")
        try:
            if (await dst.subscription("bravo_fwd_sub")).exists:
                emit("fwd_exists", side="dst")
            else:
                conninfo = await self._conninfo("fwd_conninfo", "src")
                await self._quiet(dst)
                try:
                    await dst.conn.execute(
                        sql.SQL("CREATE SUBSCRIPTION bravo_fwd_sub CONNECTION {} PUBLICATION bravo_fwd "
                                "WITH (origin = none)").format(sql.Literal(conninfo))
                    )
                finally:
                    await self._loud(dst)
                emit("fwd_created", side="dst", label=self.label)
            deadline = time.monotonic() + sync_timeout_s
            while True:
                cur = await dst.conn.execute(
                    """SELECT count(*) FILTER (WHERE r.srsubstate <> 'r'), count(*)
                       FROM pg_subscription_rel r JOIN pg_subscription s ON s.oid = r.srsubid
                       WHERE s.subname = 'bravo_fwd_sub'"""
                )
                row = await cur.fetchone()
                pending, total = (int(row[0]), int(row[1])) if row else (-1, 0)
                fwd = await dst.subscription("bravo_fwd_sub")
                if pending == 0 and total > 0 and fwd.streaming:
                    emit("fwd_streaming", tables=total, received_lsn=fwd.received_lsn)
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"forward initial sync not done (pending={pending}, streaming={fwd.streaming})")
                await asyncio.sleep(1)
            if (await src.subscription("bravo_rev_sub")).exists:
                emit("rev_exists", side="src")
            else:
                if await dst.slot("bravo_rev_sub") is None:
                    await dst.conn.execute("SELECT pg_create_logical_replication_slot('bravo_rev_sub', 'pgoutput')")
                    emit("rev_slot_created", side="dst")
                conninfo = await self._conninfo("rev_conninfo", "dst")
                await self._quiet(src)
                try:
                    await src.conn.execute(
                        sql.SQL("CREATE SUBSCRIPTION bravo_rev_sub CONNECTION {} PUBLICATION bravo_rev WITH "
                                "(enabled = false, copy_data = false, origin = none, create_slot = false, "
                                "slot_name = 'bravo_rev_sub')").format(sql.Literal(conninfo))
                    )
                except Exception as exc:
                    emit("rev_not_created", reason="reverse link unreachable or rejected",
                         error=type(exc).__name__, sqlstate=getattr(exc, "sqlstate", None))
                    raise
                finally:
                    await self._loud(src)
                emit("rev_created", side="src", enabled=False, label=self.label)
        finally:
            await src.close()
            await dst.close()

    async def rearm(self, sync_timeout_s: float) -> None:
        """Back to the pre-cutover state after a full cutover + failback, then setup()."""
        src, dst = await self._db("src"), await self._db("dst")
        try:
            for db, sub in ((dst, "bravo_fwd_sub"), (src, "bravo_rev_sub")):
                st = await db.subscription(sub)
                if st.exists:
                    if st.enabled:
                        raise RuntimeError(f"{db.side}/{sub} is enabled: rearm only after a completed failback")
                    if await db.subscription_slot_name(sub) is not None:
                        await db.conn.execute(sql.SQL("ALTER SUBSCRIPTION {} SET (slot_name = NONE)").format(
                            sql.Identifier(sub)))
                    await db.conn.execute(sql.SQL("DROP SUBSCRIPTION {}").format(sql.Identifier(sub)))
                    emit("sub_dropped", side=db.side, sub=sub)
            for db, slot in ((src, "bravo_fwd_sub"), (dst, "bravo_rev_sub")):
                if await db.slot(slot) is not None:
                    await db.conn.execute("SELECT pg_drop_replication_slot(%s)", (slot,))
                    emit("slot_dropped", side=db.side, slot=slot)
            async with dst.conn.transaction():
                await dst.conn.execute("SET LOCAL ROLE chat_owner")
                await dst.conn.execute(
                    sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(sql.Identifier(t) for t in PUBLISHED))
                )
            emit("dst_truncated", tables=list(PUBLISHED))
            for db, login in ((src, True), (dst, False)):
                await db.conn.execute(
                    sql.SQL("ALTER ROLE app_writer {}").format(sql.SQL("LOGIN" if login else "NOLOGIN"))
                )
                async with db.conn.transaction():
                    await db.conn.execute("SET LOCAL ROLE chat_owner")
                    await db.conn.execute("GRANT INSERT ON messages TO app_writer")
                store = self.topo["store_ids"][db.side]
                await db.conn.execute(
                    "UPDATE attachment_store SET active_store = %s, frozen = false, freeze_watermark = NULL "
                    "WHERE id = 1",
                    (store,),
                )
                emit("writer_reset", side=db.side, login=login, active_store=store)
        finally:
            await src.close()
            await dst.close()
        await self.setup(sync_timeout_s)


async def _main(args: argparse.Namespace) -> int:
    topo = json.loads(await asyncio.to_thread(Path(os.environ["BRAVO_TOPOLOGY_FILE"]).read_text, encoding="utf-8"))
    # In executor-0 (kubectl exec): the secrets the platform delivered; on a laptop: the AppRole.
    from .topology import FileSecrets

    secrets_dir = os.environ.get("BRAVO_SECRETS_DIR", "").strip()
    broker: Any = FileSecrets(Path(secrets_dir)) if secrets_dir else SecretBroker.from_env()
    try:
        tool = ReplSetup(topo, broker)
        if args.cmd == "status":
            emit("status", **(await tool.status()))
        elif args.cmd == "setup":
            await tool.setup(args.sync_timeout)
            emit("status", **(await tool.status()))
        elif args.cmd == "rearm":
            await tool.rearm(args.sync_timeout)
            emit("status", **(await tool.status()))
        return 0
    except Exception as exc:  # noqa: BLE001 - reported as one JSON line, non-zero exit
        # psycopg messages can quote statement text (LINE 1: ...), which may hold a connection string:
        # only the type and SQLSTATE go out for database errors.
        import psycopg

        if isinstance(exc, psycopg.Error):
            emit("failed", error=type(exc).__name__, sqlstate=exc.sqlstate)
        else:
            emit("failed", error=type(exc).__name__, detail=str(exc)[:300])
        return 1
    finally:
        await broker.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="bravo-repl-setup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=("status", "setup", "rearm"))
    p.add_argument("--sync-timeout", type=float, default=300.0)
    sys.exit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    main()


__all__ = ["AsyncConnection", "ReplSetup"]
