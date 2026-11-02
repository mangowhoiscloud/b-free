"""Test fixtures.

Database tests run against a real PostgreSQL 16 given by BRAVO_TEST_PG_DSN (a throwaway
container; scripts/run-tests.sh starts one). Each test gets its own schema holding the
contract tables (CONTRACT.md 5, tests/sql/platform_contract.sql), the Executor's own
migration (migrations/0001_resource_locks.sql) and a bravo_fence table.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

CONTRACT_SQL = Path(__file__).parent / "sql" / "platform_contract.sql"
MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))
CHAT_SCHEMA_SQL = Path(__file__).parents[3] / "apps" / "demo" / "schema" / "sql" / "01_schema.sql"
FENCE_SQL = """
CREATE TABLE bravo_fence (id int PRIMARY KEY CHECK (id = 1), epoch int NOT NULL);
INSERT INTO bravo_fence VALUES (1, 0);
CREATE TABLE fenced_probe (v int NOT NULL);
"""


@pytest.fixture
def pg_dsn() -> str:
    dsn = os.environ.get("BRAVO_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("BRAVO_TEST_PG_DSN is not set (run scripts/run-tests.sh)")
    return dsn


@pytest.fixture
async def schema_dsn(pg_dsn: str) -> AsyncIterator[str]:
    schema = f"t_{uuid.uuid4().hex[:12]}"
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        dsn = make_conninfo(pg_dsn, options=f"-c search_path={schema}")
        async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(CONTRACT_SQL.read_text(encoding="utf-8").encode())
            for migration in MIGRATIONS:
                await conn.execute(migration.read_text(encoding="utf-8").encode())
            await conn.execute(FENCE_SQL.encode())
        try:
            yield dsn
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
async def pool(schema_dsn: str) -> AsyncIterator[AsyncConnectionPool[AsyncConnection]]:
    p: AsyncConnectionPool[AsyncConnection] = AsyncConnectionPool(
        schema_dsn, connection_class=AsyncConnection, min_size=1, max_size=4, open=False
    )
    await p.open(wait=True, timeout=10)
    try:
        yield p
    finally:
        await p.close()


def _chat_schema_script() -> bytes:
    """apps/demo/schema/sql/01_schema.sql without psql meta-commands; chat_owner is cluster-wide."""
    lines = [ln for ln in CHAT_SCHEMA_SQL.read_text(encoding="utf-8").splitlines() if not ln.startswith("\\")]
    text = "\n".join(lines).replace(
        "CREATE ROLE chat_owner NOLOGIN;",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_owner') "
        "THEN CREATE ROLE chat_owner NOLOGIN; END IF; END $$;",
    )
    return text.encode()


@pytest.fixture
async def chat_dsn(pg_dsn: str) -> AsyncIterator[str]:
    """A fresh database built from the real demo chat schema (01_schema.sql), as on src/dst."""
    name = f"chat_{uuid.uuid4().hex[:12]}"
    async with await AsyncConnection.connect(pg_dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        dsn = make_conninfo(pg_dsn, dbname=name)
        try:
            async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
                await conn.execute(_chat_schema_script())
                await conn.execute("INSERT INTO attachment_store (id, active_store) VALUES (1, 'src')")
            yield dsn
        finally:
            await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
