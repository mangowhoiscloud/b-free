"""Reverse tunnel through real processes: client -> relay -> agent -> PostgreSQL container."""

from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import time

import psycopg
import pytest
from conftest import PgContainer, Tunnel, relay_stats

MIB = 1024 * 1024


def test_queries_parallel_connections_and_bytes(publisher: PgContainer, tunnel: Tunnel) -> None:
    dsn = publisher.dsn(port=tunnel.client_port)
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert conn.execute("SELECT inet_server_port()").fetchone() == (5432,)

    def worker(i: int) -> int:
        with psycopg.connect(dsn) as c:
            n = 0
            for j in range(25):
                row = c.execute("SELECT md5(repeat(%s, 1000))", (f"{i}-{j}",)).fetchone()
                assert row is not None and row[0] == hashlib.md5(f"{i}-{j}".encode() * 1000).hexdigest()  # noqa: S324
                n += 1
            return n

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        done = sum(pool.map(worker, range(64)))
    parallel_s = time.monotonic() - t0
    assert done == 64 * 25

    with psycopg.connect(dsn) as conn:
        t0 = time.monotonic()
        row = conn.execute("SELECT repeat('x', %s)", (16 * MIB,)).fetchone()
        down_s = time.monotonic() - t0
        assert row is not None and len(row[0]) == 16 * MIB
        conn.execute("CREATE TEMP TABLE up (b text)")
        payload = ("y" * 1023 + "\n") * (16 * 1024)  # 16 MiB in 1 KiB lines
        t0 = time.monotonic()
        with conn.cursor().copy("COPY up FROM STDIN") as cp:
            buf = io.StringIO(payload)
            while chunk := buf.read(256 * 1024):
                cp.write(chunk)
        up_s = time.monotonic() - t0
        got = conn.execute("SELECT count(*), sum(length(b)) FROM up").fetchone()
        assert got == (16 * 1024, 16 * 1024 * 1023)

    stats = relay_stats(tunnel.agent_port)
    assert int(str(stats["bytes_up"])) >= 16 * MIB and int(str(stats["bytes_down"])) >= 16 * MIB
    assert int(str(stats["clients_rejected_no_agent"])) == 0
    status = json.loads(tunnel.status_file.read_text())
    assert status["label"] == "임시 링크(D-16 전)" and status["state"] == "up"
    print(json.dumps({
        "parallel_connections": 64, "queries": done, "parallel_s": round(parallel_s, 2),
        "down_16MiB_s": round(down_s, 2), "up_16MiB_s": round(up_s, 2),
        "relay": {k: stats[k] for k in ("clients_paired", "bytes_up", "bytes_down", "agents_registered")},
    }))


def test_agent_loss_rejects_then_reconnects(publisher: PgContainer, tunnel: Tunnel) -> None:
    dsn = publisher.dsn(port=tunnel.client_port)
    held = psycopg.connect(dsn)
    assert held.execute("SELECT 1").fetchone() == (1,)
    tunnel.agent.kill()
    with pytest.raises(psycopg.OperationalError):
        held.execute("SELECT 1")  # its splice lived in the agent process
    held.close()
    t0 = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(dsn)  # no idle agent connection: closed after --wait-agent-timeout
    rejected_after_s = time.monotonic() - t0
    tunnel.restart_agent()
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    stats = relay_stats(tunnel.agent_port)
    assert int(str(stats["clients_rejected_no_agent"])) >= 1
    print(json.dumps({"rejected_after_s": round(rejected_after_s, 2), "relay": stats}))
