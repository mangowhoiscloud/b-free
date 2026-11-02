"""Shared fixtures. PostgreSQL tests run against a real postgres:16 container (no mocks)."""

from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

PG_IMAGE = "postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


@dataclass(frozen=True)
class PgServer:
    host: str
    port: int
    superuser_dsn: str
    collector_dsn: str


def _docker_ok() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not found"
    r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,  # noqa: S603,S607
                       timeout=20, check=False)
    return None if r.returncode == 0 else f"docker daemon unavailable: {r.stderr.strip()[:200]}"


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    reason = _docker_ok()
    if reason:
        pytest.skip(f"PostgreSQL tests need a real postgres:16 container: {reason}")
    su_pw = secrets.token_urlsafe(24)
    ro_pw = secrets.token_urlsafe(24)
    run = subprocess.run(  # noqa: S603
        ["docker", "run", "-d", "--rm", "-P", "-e", f"POSTGRES_PASSWORD={su_pw}", "-e", "POSTGRES_DB=chat",  # noqa: S607
         PG_IMAGE],
        capture_output=True, text=True, timeout=300, check=True,
    )
    cid = run.stdout.strip()
    try:
        port_out = subprocess.run(["docker", "port", cid, "5432/tcp"], capture_output=True, text=True,  # noqa: S603,S607
                                  timeout=20, check=True).stdout
        port = int(port_out.splitlines()[0].rsplit(":", 1)[1])
        su = f"host=127.0.0.1 port={port} dbname=chat user=postgres password={su_pw}"
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(su, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        # Apply the shipped SQL with psql inside the container (same path operators use).
        sql = (SQL_DIR / "collector_ro.sql").read_text(encoding="utf-8")
        subprocess.run(  # noqa: S603
            ["docker", "exec", "-i", cid, "psql", "-U", "postgres", "-d", "chat", "-v", "ON_ERROR_STOP=1",  # noqa: S607
             "-v", f"collector_ro_password={ro_pw}", "-q"],
            input=sql, capture_output=True, text=True, timeout=60, check=True,
        )
        with psycopg.connect(su, autocommit=True) as conn:
            conn.execute("CREATE TABLE messages (id bigserial PRIMARY KEY, body text NOT NULL)")
            conn.execute("CREATE ROLE app_writer LOGIN PASSWORD NULL")
            conn.execute("GRANT SELECT, INSERT ON messages TO app_writer")
            conn.execute("GRANT SELECT ON messages TO PUBLIC")  # planted regression to be reported
        ro = f"host=127.0.0.1 port={port} dbname=chat user=collector_ro password={ro_pw}"
        yield PgServer(host="127.0.0.1", port=port, superuser_dsn=su, collector_dsn=ro)
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60, check=False)  # noqa: S603,S607
