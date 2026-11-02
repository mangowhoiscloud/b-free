"""Logical replication streaming through the reverse tunnel (two PostgreSQL containers).

subscriber container -> host.docker.internal:<relay client port> -> relay -> agent -> publisher.
This is the shape of the reverse subscription in EKS reaching the K3s DB.
"""

from __future__ import annotations

import json
import time

import psycopg
from conftest import PgContainer, Tunnel, relay_stats


def _count(dsn: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as c:
        row = c.execute("SELECT count(*) FROM t").fetchone()
        return int(row[0]) if row else 0


def _wait_count(dsn: str, want: int, timeout_s: float) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if _count(dsn) >= want:
            return time.monotonic() - t0
        time.sleep(0.05)
    raise TimeoutError(f"subscriber has {_count(dsn)} rows, want {want}")


def _insert(dsn: str, start: int, n: int) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO t SELECT g, md5(g::text) FROM generate_series(%s::bigint, %s::bigint) g",
            (start, start + n - 1),
        )


def test_subscription_streams_through_reverse_tunnel(
    publisher: PgContainer, subscriber: PgContainer, tunnel: Tunnel
) -> None:
    pub, sub = publisher.dsn(), subscriber.dsn()
    with psycopg.connect(pub, autocommit=True) as c:
        c.execute("CREATE TABLE t (id bigint PRIMARY KEY, v text NOT NULL)")
        c.execute("CREATE PUBLICATION p FOR TABLE t")
    _insert(pub, 1, 5000)
    conninfo = (f"host=host.docker.internal port={tunnel.client_port} dbname=postgres user=postgres "
                f"password={publisher.password} sslmode=disable")
    with psycopg.connect(sub, autocommit=True) as c:
        c.execute("CREATE TABLE t (id bigint PRIMARY KEY, v text NOT NULL)")
        c.execute(f"CREATE SUBSCRIPTION s CONNECTION '{conninfo}' PUBLICATION p")  # noqa: S608 - test DSN
    initial_s = _wait_count(sub, 5000, 60)

    t0 = time.monotonic()
    _insert(pub, 5001, 20000)
    insert_s = time.monotonic() - t0
    stream_s = _wait_count(sub, 25000, 60)

    with psycopg.connect(pub, autocommit=True) as c:
        row = c.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) FROM pg_replication_slots "
            "WHERE slot_name = 's'"
        ).fetchone()
    slot_lag_bytes = int(row[0]) if row and row[0] is not None else None

    # Agent dies: replication stops; the agent comes back: the apply worker reconnects and catches up.
    tunnel.agent.kill()
    _insert(pub, 25001, 5000)
    time.sleep(2)
    during_outage = _count(sub)
    assert during_outage == 25000, "replication must stop while the agent is down"
    t0 = time.monotonic()
    tunnel.restart_agent()
    _wait_count(sub, 30000, 90)
    catch_up_s = time.monotonic() - t0

    with psycopg.connect(pub) as c1, psycopg.connect(sub) as c2:
        q = "SELECT md5(string_agg(id::text || v, ',' ORDER BY id)) FROM t"
        assert c1.execute(q).fetchone() == c2.execute(q).fetchone()
    stats = relay_stats(tunnel.agent_port)
    print(json.dumps({
        "initial_sync_5000_rows_s": round(initial_s, 2),
        "stream_20000_rows": {"insert_s": round(insert_s, 2), "visible_on_subscriber_s": round(stream_s, 2)},
        "slot_lag_bytes_after_stream": slot_lag_bytes,
        "agent_outage": {
            "rows_on_subscriber_during_outage": during_outage, "catch_up_5000_rows_s": round(catch_up_s, 2)
        },
        "relay": {k: stats[k] for k in ("clients_paired", "bytes_up", "bytes_down", "agents_dead_on_open")},
    }))
