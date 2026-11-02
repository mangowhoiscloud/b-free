"""Throwaway postgres:16 with the real platform/api migrations (same image pin as platform/api tests)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

API_SRC = Path(__file__).resolve().parents[2] / "api" / "src"
PG_IMAGE = os.environ.get(
    "BRAVO_TEST_PG_IMAGE",
    "postgres:16.13@sha256:5d143123fdf80462d1778cd4f24b9f7ca13c87174bca19141fb194c5a1ebca59",
)
PASSWORDS = {"bravo_api": "api-pw", "bravo_apply": "apply-pw"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg() -> Iterator[dict[str, str]]:
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker_unavailable: the DB tests need a throwaway postgres:16 container")
    sys.path.insert(0, str(API_SRC))
    from bravo_api.migrate import apply as migrate  # the real migrations, 0001..000N

    port, name = _free_port(), f"bravo-apply-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_USER=bravo_owner",
                    "-e", "POSTGRES_PASSWORD=owner-pw", "-e", "POSTGRES_DB=bravo", "-p", f"127.0.0.1:{port}:5432", PG_IMAGE],
                   check=True, capture_output=True)
    owner = f"postgresql://bravo_owner:owner-pw@127.0.0.1:{port}/bravo"
    try:
        deadline = time.time() + 60
        while True:
            try:
                with psycopg.connect(owner, connect_timeout=2) as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        migrate(owner)
        with psycopg.connect(owner, autocommit=True) as c:
            for role, pw in PASSWORDS.items():
                c.execute(f"ALTER ROLE {role} PASSWORD '{pw}'")
        base = f"127.0.0.1:{port}/bravo"
        yield {"owner": owner, **{r: f"postgresql://{r}:{pw}@{base}" for r, pw in PASSWORDS.items()}}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
