"""repl_setup against two real PostgreSQL 16 containers on one Docker network, as executor_ctl
(no superuser): forward subscription with initial copy, prepared reverse subscription, rearm.

Needs Docker and BRAVO_TEST_PG_IMAGE (scripts/run-tests.sh sets it); skipped otherwise.
"""
# ruff: noqa: S603, S607 - the test drives the docker CLI with fixed arguments

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import AsyncConnection

from bravo_executor.repl_setup import ReplSetup

REPO = Path(__file__).parents[3]
sys.path.insert(0, str(REPO / "deploy" / "cloud"))
from db_roles import scram_verifier  # noqa: E402

ROLES = ("app_writer", "executor_ctl", "executor_observe", "replicator")


def _docker(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["docker", *args], input=stdin, text=True, capture_output=True, timeout=120, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"docker {args[0]}: {r.stderr[-300:]}")
    return r.stdout


@pytest.fixture(scope="module")
def pair() -> Iterator[dict[str, Any]]:
    image = os.environ.get("BRAVO_TEST_PG_IMAGE", "")
    if not image or shutil.which("docker") is None:
        pytest.skip("needs docker and BRAVO_TEST_PG_IMAGE")
    tag = uuid.uuid4().hex[:8]
    net = f"bravo-repl-{tag}"
    pw = {side: {r: secrets.token_urlsafe(18) for r in ROLES} for side in ("src", "dst")}
    names = {side: f"bravo-repl-{side}-{tag}" for side in ("src", "dst")}
    ports: dict[str, int] = {}
    _docker("network", "create", net)
    try:
        for side in ("src", "dst"):
            _docker("run", "-d", "--name", names[side], "--network", net, "-e", "POSTGRES_PASSWORD=unused-superuser",
                    "-e", "POSTGRES_DB=chat", "-p", "127.0.0.1::5432", image, "-c", "wal_level=logical")
        for side in ("src", "dst"):
            for _ in range(60):
                if subprocess.run(["docker", "exec", names[side], "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                  capture_output=True, check=False).returncode == 0:
                    break
                subprocess.run(["sleep", "1"], check=False)
            head = "".join(f"\\set {r}_verifier '{scram_verifier(pw[side][r])}'\n" for r in ROLES)
            head += f"\\set attachment_store_id '{side}'\n\\set plant_regression false\n"
            body = "".join((REPO / "apps/demo/schema/sql" / f).read_text()
                           for f in ("01_schema.sql", "02_roles.sql", f"03_{side}.sql"))
            _docker("exec", "-i", names[side], "psql", "-U", "postgres", "-d", "chat", "-v", "ON_ERROR_STOP=1", "-q",
                    "-f", "-", stdin=head + body)
            ports[side] = int(_docker("port", names[side], "5432").split(":")[-1])
        topo = {
            "label": "test link", "chat_db": "chat", "store_ids": {"src": "src", "dst": "dst"},
            "db": {side: {"conninfo": f"host=127.0.0.1 port={ports[side]}", "secret": f"envs/{side}/chat-db"}
                   for side in ("src", "dst")},
            "replication": {"fwd_conninfo": f"host={names['src']} port=5432",
                            "rev_conninfo": f"host={names['dst']} port=5432"},
        }
        yield {"topo": topo, "pw": pw, "ports": ports}
    finally:
        for n in names.values():
            subprocess.run(["docker", "rm", "-f", n], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", net], capture_output=True, check=False)


class DictSecrets:
    """The OpenBao lookup the tool uses (path, field) -> value, filled from the test's own passwords."""

    def __init__(self, pw: dict[str, dict[str, str]]) -> None:
        self._pw = pw

    async def field(self, path: str, name: str) -> str:
        return self._pw[path.split("/")[1]][name]

    async def close(self) -> None:
        return None


@pytest.fixture
async def tool(pair: dict[str, Any]) -> AsyncIterator[ReplSetup]:
    yield ReplSetup(pair["topo"], DictSecrets(pair["pw"]))  # type: ignore[arg-type]


async def _q(pair: dict[str, Any], side: str, sql: str) -> Any:
    dsn = (f"host=127.0.0.1 port={pair['ports'][side]} dbname=chat user=executor_ctl "
           f"password={pair['pw'][side]['executor_ctl']}")
    async with await AsyncConnection.connect(dsn, autocommit=True) as c:
        return await (await c.execute(sql)).fetchone()


async def test_setup_streams_forward_and_prepares_reverse(pair: dict[str, Any], tool: ReplSetup) -> None:
    app = (f"host=127.0.0.1 port={pair['ports']['src']} dbname=chat user=app_writer "
           f"password={pair['pw']['src']['app_writer']}")
    async with await AsyncConnection.connect(app, autocommit=True) as c:
        for i in range(25):
            await c.execute("INSERT INTO messages (room, author, body, idem_key) VALUES ('r', 'a', %s, %s)",
                            (f"m{i}", uuid.uuid4()))
    await tool.setup(sync_timeout_s=60)
    for _ in range(50):
        row = await _q(pair, "dst", "SELECT count(*) FROM messages")
        if row and row[0] == 25:
            break
        await asyncio.sleep(0.2)
    assert row is not None and row[0] == 25
    rev = await _q(pair, "src", "SELECT subenabled, subslotname FROM pg_subscription WHERE subname = 'bravo_rev_sub'")
    assert rev == (False, "bravo_rev_sub")
    rel = await _q(pair, "src", "SELECT count(*) FROM pg_subscription_rel r JOIN pg_subscription s "
                                "ON s.oid = r.srsubid WHERE s.subname = 'bravo_rev_sub'")
    assert rel is not None and rel[0] == 4  # connect = true recorded the reverse tables
    status = await tool.status()
    assert status["dst"]["subscriptions"]["bravo_fwd_sub"]["worker_pid"] is not None


async def test_setup_is_idempotent(tool: ReplSetup) -> None:
    await tool.setup(sync_timeout_s=60)  # second run: both subscriptions exist, nothing recreated
