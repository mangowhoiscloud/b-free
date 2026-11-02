"""Operator commands on the cluster topology (runbook, fault injection, Exit Drill). JSON output.

  python -m bravo_executor.ops entry <name> get|block|unblock   # demo app write-block switch
  python -m bravo_executor.ops pool <name> current               # side of the PgBouncer active file
  python -m bravo_executor.ops pool <name> show                  # SHOW DATABASES row (stats user)
  python -m bravo_executor.ops pool <name> resume                # RESUME as pgb_admin (runbook), then SHOW
  python -m bravo_executor.ops pool <name> hold                  # keep one admin session open 180 s (E6)
  python -m bravo_executor.ops db <side> fence                   # bravo_fence epoch (observe role)
  python -m bravo_executor.ops db <side> e4                      # E4: observe/stats credentials cannot write

Run under ``executor-bao.sh with-executor`` with ``BRAVO_TOPOLOGY_FILE``. These are direct
operator actions, not Executor runs: they write no effect rows, and the caller records them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .entry import WriteBlockClient
from .topology import settings_from_topology
from .vault import SecretBroker


async def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("entry", "pool", "db"):
        print(__doc__, file=sys.stderr)
        return 2
    kind, name, action = argv
    broker = SecretBroker.from_env()
    try:
        s = await settings_from_topology(Path(os.environ["BRAVO_TOPOLOGY_FILE"]), broker.field, "unused",
                                         require_entries=False)
    finally:
        await broker.close()
    out: dict[str, Any] = {"kind": kind, "name": name, "action": action}
    if kind == "db":
        dsn = s.src_observe_dsn if name == "src" else s.dst_observe_dsn
        if action == "fence":
            async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
                row = await (await conn.execute("SELECT epoch FROM bravo_fence WHERE id = 1")).fetchone()
                out["epoch"] = None if row is None else int(row[0])
        elif action == "e4":
            out["db"] = await _e4_db(dsn)
            stats = dict(s.pool_stats_dsns).get(name)
            out["pgbouncer"] = await _e4_pgb(stats) if stats else "no pool for this side"
            out["all_refused"] = all(v["refused"] for v in out["db"].values()) and (
                not isinstance(out["pgbouncer"], dict) or all(v["refused"] for v in out["pgbouncer"].values()))
        else:
            return 2
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    if kind == "entry":
        entry = next((e for e in s.entries if e.name == name), None)
        if entry is None:
            print(json.dumps({**out, "error": "no such entry"}))
            return 2
        client = WriteBlockClient(entry)
        if action == "get":
            out["state"] = await client.get()
        elif action in ("block", "unblock"):
            out["before"] = await client.get()
            out["state"] = await client.set(action == "block")
        else:
            return 2
    else:
        pool = next((p for p in s.pools if p.name == name), None)
        if pool is None:
            print(json.dumps({**out, "error": "no such pool"}))
            return 2
        if action not in ("current", "resume", "show", "hold"):
            return 2
        if action == "hold":
            async with await AsyncConnection.connect(pool.admin_dsn, autocommit=True) as conn:
                await conn.execute(b"SHOW VERSION")
                await asyncio.sleep(180)
            out["held_s"] = 180
        if action == "current":
            out["active_side"] = await pool.switch.current_side()
            out["files"] = await pool.switch.preflight()
        if action == "resume":
            async with await AsyncConnection.connect(pool.admin_dsn, autocommit=True) as conn:
                await conn.execute(f"RESUME {pool.db_name}".encode())
            out["resume_sent"] = True
        if action in ("resume", "show"):
            dsn = dict(s.pool_stats_dsns)[name]
            async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(b"SHOW DATABASES")
                    out["row"] = next((
                        {k: r.get(k) for k in ("name", "host", "port", "paused", "current_connections")}
                        for r in await cur.fetchall() if r.get("name") == pool.db_name), None)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


E4_DB = {
    "fence update": "UPDATE bravo_fence SET epoch = epoch WHERE id = 1",
    "barrier insert": "INSERT INTO bravo_barrier (plan_id, epoch) VALUES ('e4', 0)",
    "alter role": "ALTER ROLE app_writer NOLOGIN",
    "revoke": "REVOKE INSERT ON messages FROM app_writer",
    "subscription": "ALTER SUBSCRIPTION bravo_rev_sub DISABLE",
    "attachment freeze": "UPDATE attachment_store SET frozen = true WHERE id = 1",
}


async def _e4_db(dsn: str) -> dict[str, Any]:
    """Each statement must fail with a privilege error; a REVOKE that "succeeds" with no effect is
    checked by reading the privilege afterwards."""
    out: dict[str, Any] = {}
    async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
        for label, stmt in E4_DB.items():
            try:
                await conn.execute(stmt.encode())
                refused, detail = False, "statement succeeded"
            except Exception as exc:  # noqa: BLE001 - the refusal is the expected outcome
                refused, detail = True, f"{getattr(exc, 'sqlstate', None)} {str(exc).splitlines()[0][:120]}"
            out[label] = {"refused": refused, "detail": detail}
        row = await (await conn.execute(
            "SELECT has_table_privilege('app_writer', 'messages', 'INSERT')")).fetchone()
        if not out["revoke"]["refused"]:
            still = bool(row and row[0])
            out["revoke"] = {"refused": still, "detail": f"app_writer INSERT still {still}"}
    return out


async def _e4_pgb(dsn: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
        for label, stmt in (("pause", b"PAUSE"), ("reload", b"RELOAD")):
            try:
                await conn.execute(stmt)
                out[label] = {"refused": False, "detail": "command accepted"}
            except Exception as exc:  # noqa: BLE001
                out[label] = {"refused": True, "detail": str(exc).splitlines()[0][:120]}
    return out


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
