"""Open-loop chat write load through PgBouncer.

Each logical request has a request ID that is also the message ``idem_key``. Every
attempt has its own attempt ID. On a connection error, timeout or pause, the request
is retried with the same idem_key; ``ON CONFLICT (idem_key) DO NOTHING`` turns a
retry of an already-committed attempt into a lookup, so a lost ACK never duplicates
a message. Requests are scheduled at a fixed rate whatever the latency (open loop),
so a write pause shows up as queueing latency, not as a lower offered rate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import AsyncConnection

from .stats import RunStats

log = logging.getLogger("bravo_loadgen")

INSERT_SQL = """
INSERT INTO messages (room, author, body, idem_key)
VALUES (%s, %s, %s, %s)
ON CONFLICT (idem_key) DO NOTHING
RETURNING id
"""
LOOKUP_SQL = "SELECT id FROM messages WHERE idem_key = %s"


@dataclass(frozen=True, slots=True)
class LoadConfig:
    dsn: str
    rate: float
    duration_s: float
    concurrency: int
    attempt_timeout_s: float
    max_attempts: int
    drain_timeout_s: float
    room: str
    author: str


@dataclass(slots=True)
class Request:
    request_id: uuid.UUID
    created_at: float
    attempts: int = 0


@dataclass(slots=True)
class Result:
    stats: RunStats = field(default_factory=RunStats)
    acked: list[tuple[uuid.UUID, int]] = field(default_factory=list)


def _event(kind: str, **fields: Any) -> None:
    sys.stderr.write(json.dumps({"event": kind, "ts": round(time.time(), 3), **fields}) + "\n")


class LoadGenerator:
    def __init__(self, cfg: LoadConfig) -> None:
        self.cfg = cfg
        self.result = Result()
        self._queue: asyncio.Queue[Request | None] = asyncio.Queue()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _connect(self) -> AsyncConnection:
        return await AsyncConnection.connect(
            self.cfg.dsn,
            autocommit=True,
            prepare_threshold=None,  # PgBouncer transaction mode
            connect_timeout=max(1, int(self.cfg.attempt_timeout_s)),
            application_name="bravo-loadgen",
        )

    async def _producer(self) -> None:
        start = time.monotonic()
        interval = 1.0 / self.cfg.rate
        n = 0
        while not self._stop.is_set():
            due = start + n * interval
            if due - start >= self.cfg.duration_s:
                break
            delay = due - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break
                except TimeoutError:
                    pass
            self._queue.put_nowait(Request(request_id=uuid.uuid4(), created_at=time.monotonic()))
            self.result.stats.sent += 1
            n += 1
        for _ in range(self.cfg.concurrency):
            self._queue.put_nowait(None)

    async def _attempt(self, conn: AsyncConnection, req: Request) -> tuple[int, bool]:
        body = f"load message {req.request_id}"
        cur = await conn.execute(INSERT_SQL, (self.cfg.room, self.cfg.author, body, req.request_id))
        row = await cur.fetchone()
        if row is not None:
            return int(row[0]), False
        cur = await conn.execute(LOOKUP_SQL, (req.request_id,))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("conflict on idem_key but row not visible")
        return int(row[0]), True

    async def _worker(self, worker_id: int) -> None:
        conn: AsyncConnection | None = None
        stats = self.result.stats
        while True:
            req = await self._queue.get()
            if req is None:
                break
            while True:
                req.attempts += 1
                attempt_id = uuid.uuid4()
                try:
                    if conn is None or conn.closed:
                        conn = await asyncio.wait_for(self._connect(), self.cfg.attempt_timeout_s)
                    msg_id, on_conflict = await asyncio.wait_for(
                        self._attempt(conn, req), self.cfg.attempt_timeout_s
                    )
                except (psycopg.Error, TimeoutError, OSError, RuntimeError) as exc:
                    now = time.monotonic()
                    stats.first_error_at = stats.first_error_at or now
                    stats.last_error_at = now
                    stats.retry_attempts += 1
                    if req.attempts == 1:
                        stats.retried_requests += 1
                    _event(
                        "attempt_failed",
                        request_id=str(req.request_id),
                        attempt_id=str(attempt_id),
                        attempt=req.attempts,
                        worker=worker_id,
                        error=type(exc).__name__,
                        sqlstate=getattr(exc, "sqlstate", None),
                    )
                    if conn is not None:
                        try:
                            await asyncio.wait_for(conn.close(), 2)
                        except Exception:  # noqa: BLE001, S110 - connection already broken
                            pass
                        conn = None
                    if req.attempts >= self.cfg.max_attempts:
                        stats.failed += 1
                        _event("request_failed", request_id=str(req.request_id), attempts=req.attempts)
                        break
                    await asyncio.sleep(min(0.05 * 2 ** (req.attempts - 1), 1.0))
                    continue
                now = time.monotonic()
                latency_ms = (now - req.created_at) * 1000
                stats.record_ack(now, latency_ms, on_conflict)
                self.result.acked.append((req.request_id, msg_id))
                if req.attempts > 1 or on_conflict:
                    _event(
                        "acked_after_retry",
                        request_id=str(req.request_id),
                        attempt_id=str(attempt_id),
                        attempt=req.attempts,
                        id=msg_id,
                        on_conflict=on_conflict,
                        latency_ms=round(latency_ms, 1),
                    )
                break
        if conn is not None:
            await conn.close()

    async def run(self) -> Result:
        workers = [asyncio.create_task(self._worker(i)) for i in range(self.cfg.concurrency)]
        await self._producer()
        try:
            await asyncio.wait_for(asyncio.gather(*workers), self.cfg.duration_s + self.cfg.drain_timeout_s)
        except TimeoutError:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # Requests still queued or in flight were never acked: count them as failed.
            in_queue = sum(1 for _ in range(self._queue.qsize()) if self._queue.get_nowait() is not None)
            outstanding = self.result.stats.sent - self.result.stats.acked - self.result.stats.failed - in_queue
            self.result.stats.failed += in_queue + max(0, outstanding)
        return self.result


async def count_keys(dsn: str, keys: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    counts: dict[uuid.UUID, int] = {}
    async with await AsyncConnection.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            cur = await conn.execute(
                "SELECT idem_key, count(*) FROM messages WHERE idem_key = ANY(%s) GROUP BY idem_key", (chunk,)
            )
            for key, n in await cur.fetchall():
                counts[key] = int(n)
    return counts
