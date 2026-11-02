"""Fixes from the managed-DB spikes (docs/architecture/12-db-migration-matrix.md 11), each on real
processes: a PgBouncer container for the admin console, a real PostgreSQL for sessions and logs."""
# ruff: noqa: S603, S607 - the tests drive the docker CLI with fixed arguments

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection, sql

from bravo_executor.chatdb import ChatDb, connect
from bravo_executor.config import ConfigError
from bravo_executor.pgbouncer import PgBouncerAdmin
from bravo_executor.topology import settings_from_topology

PGB_IMAGE = os.environ.get("BRAVO_TEST_PGB_IMAGE", "bravo-pgbouncer:pilot")


def _docker(*args: str) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"docker {args[0]}: {r.stderr[-300:]}")
    return r.stdout


@pytest.fixture(scope="module")
def pgbouncer() -> Iterator[dict[str, Any]]:
    image = os.environ.get("BRAVO_TEST_PG_IMAGE", "")
    if not image or shutil.which("docker") is None:
        pytest.skip("needs docker and BRAVO_TEST_PG_IMAGE")
    if subprocess.run(["docker", "image", "inspect", PGB_IMAGE], capture_output=True, check=False).returncode:
        pytest.skip(f"PgBouncer image {PGB_IMAGE} not built (deploy/pilot/pgbouncer/Dockerfile)")
    tag = uuid.uuid4().hex[:8]
    net, pg, pgb = f"bravo-pgb-{tag}", f"bravo-pgb-pg-{tag}", f"bravo-pgb-pgb-{tag}"
    cfg = Path(f"/tmp/bravo-pgb-{tag}")  # noqa: S108 - throwaway test config, removed below
    cfg.mkdir()
    pw = secrets.token_urlsafe(12)
    try:
        _docker("network", "create", net)
        _docker("run", "-d", "--name", pg, "--network", net, "-e", f"POSTGRES_PASSWORD={pw}", image)
        (cfg / "pgbouncer.ini").write_text(
            f"[databases]\nchat = host={pg} port=5432 dbname=postgres\n[pgbouncer]\nlisten_addr = 0.0.0.0\n"
            "listen_port = 6432\nunix_socket_dir =\nauth_type = plain\nauth_file = /cfg/userlist.txt\n"
            "pool_mode = transaction\nadmin_users = pgb_admin\nmax_prepared_statements = 0\n")
        (cfg / "userlist.txt").write_text(f'"postgres" "{pw}"\n"pgb_admin" "{pw}"\n')
        for f in cfg.iterdir():
            f.chmod(0o644)
        _docker("run", "-d", "--name", pgb, "--network", net, "-p", "127.0.0.1::6432", "-v", f"{cfg}:/cfg:ro",
                "-e", "PGBOUNCER_INI=/cfg/pgbouncer.ini", PGB_IMAGE)
        port = int(_docker("port", pgb, "6432").split(":")[-1])
        subprocess.run(["sleep", "3"], check=False)
        yield {"admin": f"host=127.0.0.1 port={port} dbname=pgbouncer user=pgb_admin password={pw}"}
    finally:
        for n in (pgb, pg):
            subprocess.run(["docker", "rm", "-f", n], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", net], capture_output=True, check=False)
        shutil.rmtree(cfg, ignore_errors=True)


async def test_admin_console_survives_many_identical_commands(pgbouncer: dict[str, Any]) -> None:
    """psycopg prepares the 6th identical statement by default; the admin console rejects that."""
    admin = await PgBouncerAdmin.connect(pgbouncer["admin"])
    try:
        for _ in range(12):
            assert await admin.database("chat") is not None
        for _ in range(8):
            await admin.command("PAUSE", "chat")
            await admin.command("RESUME", "chat")
        guard = await admin.wait_no_logins("chat", timeout_s=2)
        assert guard["logging_in"] == 0
    finally:
        await admin.close()


async def test_ending_app_sessions_never_ends_the_control_session(chat_dsn: str, pg_dsn: str) -> None:
    role, pw = f"app_{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(12)
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as su:
        await su.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(pw)))
    try:
        app_dsn = psycopg.conninfo.make_conninfo(chat_dsn, user=role, password=pw)
        apps = [await AsyncConnection.connect(app_dsn, autocommit=True) for _ in range(3)]
        control = ChatDb(await connect(chat_dsn), "src")
        ended = await control.terminate_role_sessions(epoch=1, role=role)
        assert ended == 3
        assert (await (await control.conn.execute("SELECT 1")).fetchone()) == (1,)
        for a in apps:
            with pytest.raises(psycopg.OperationalError):
                await a.execute("SELECT 1")
        await control.close()
    finally:
        async with await AsyncConnection.connect(pg_dsn, autocommit=True) as su:
            await su.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_topology_refuses_a_shared_control_and_app_endpoint(tmp_path: Path) -> None:
    topo = {"chat_db": "chat", "store_ids": {"src": "src", "dst": "dst"},
            "db": {"src": {"conninfo": "host=link port=5432", "secret": "a"},
                   "dst": {"conninfo": "host=dst port=5432", "secret": "b"}},
            "pools": [{"name": "src", "conninfo": "host=link port=5432", "secret": "a",
                       "switch": {"kind": "k8s-exec", "context": "c", "namespace": "n", "workload": "pod/p"}}]}
    f = tmp_path / "t.json"
    f.write_text(__import__("json").dumps(topo))

    async def lookup(path: str, name: str) -> str:
        return "x"

    with pytest.raises(ConfigError):
        await settings_from_topology(f, lookup, "unused")


async def test_digest_runs_whatever_the_database_datestyle(chat_dsn: str, pg_dsn: str) -> None:
    dbname = psycopg.conninfo.conninfo_to_dict(chat_dsn)["dbname"]
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as su:
        await su.execute(sql.SQL("ALTER DATABASE {} SET DateStyle = 'SQL, DMY'").format(sql.Identifier(str(dbname))))
        await su.execute(sql.SQL("ALTER DATABASE {} SET TimeZone = 'Asia/Seoul'").format(sql.Identifier(str(dbname))))
    async with await AsyncConnection.connect(chat_dsn, autocommit=True) as raw:
        await raw.execute("INSERT INTO messages (room, author, body, idem_key) VALUES ('r', 'a', 'b', %s)",
                          (uuid.uuid4(),))
        with pytest.raises((psycopg.DataError, NotImplementedError)):  # the spike: non-ISO dates do not parse
            await (await raw.execute("SELECT created_at FROM messages")).fetchone()
    db = ChatDb(await connect(chat_dsn), "src")
    try:
        d = await db.table_digest("messages")
        assert d.rows == 1
        style = await (await db.conn.execute("SHOW DateStyle")).fetchone()
        assert style == ("ISO, MDY",)
    finally:
        await db.close()
