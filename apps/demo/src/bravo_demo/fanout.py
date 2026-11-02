"""Message fan-out by polling the shared write database (§8.10).

Every app instance, in any environment, polls ``messages`` for ids above what it has seen and
pushes new rows to its own WebSocket clients. There is no in-memory or per-environment pub/sub
between instances and no LISTEN/NOTIFY, so during a traffic shift the AWS and on-prem instances
both see every message written through PgBouncer.

Sequence ids are allocated before commit, so a row can become visible after a higher id
(late commit). ``PollCursor`` keeps the unseen ids below the highest seen id as open gaps and
re-reads them until they appear or until ``grace_s`` passes (a write transaction lasts at most
200 ms; gaps from rollbacks and ``ON CONFLICT DO NOTHING`` never fill).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_OPEN_GAPS = 64


class PollCursor:
    def __init__(self, grace_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.grace_s = grace_s
        self.clock = clock
        self.watermark = 0  # every id <= watermark was delivered or given up
        self.high = 0  # highest id seen
        self.seen: set[int] = set()  # delivered ids in (watermark, high]
        self.gaps: list[list[float]] = []  # [lo, hi, since]: unseen ids in (watermark, high], sorted

    def start_from(self, ids: list[int]) -> None:
        """Initialise from existing ids without delivering them (clients read history themselves)."""
        if not ids:
            return
        ids = sorted(ids)
        self.watermark = ids[0] - 1
        self.high = self.watermark
        self.accept(ids)

    def open_gaps(self) -> list[tuple[int, int]]:
        return [(int(g[0]), int(g[1])) for g in self.gaps]

    def accept(self, ids: list[int]) -> list[int]:
        """Record polled ids; return the ones not delivered before, in id order."""
        now = self.clock()
        fresh: list[int] = []
        for i in sorted(set(ids)):
            if i <= self.watermark or i in self.seen:
                continue
            if i > self.high:
                if i > self.high + 1:
                    self.gaps.append([self.high + 1, i - 1, now])
                self.high = i
            else:
                self._fill(i)
            self.seen.add(i)
            fresh.append(i)
        self._advance(now)
        return fresh

    def _fill(self, i: int) -> None:
        for idx, (lo, hi, since) in enumerate(self.gaps):
            if lo <= i <= hi:
                parts = []
                if lo <= i - 1:
                    parts.append([lo, i - 1, since])
                if i + 1 <= hi:
                    parts.append([i + 1, hi, since])
                self.gaps[idx : idx + 1] = parts
                return

    def _advance(self, now: float) -> None:
        # Expire old gaps, and cap how many stay open.
        self.gaps = [g for g in self.gaps if now - g[2] < self.grace_s]
        if len(self.gaps) > MAX_OPEN_GAPS:
            self.gaps = self.gaps[-MAX_OPEN_GAPS:]
        floor = int(self.gaps[0][0]) - 1 if self.gaps else self.high
        if floor > self.watermark:
            self.watermark = floor
            self.seen = {i for i in self.seen if i > floor}


class Hub:
    """Local WebSocket subscribers by room. Fed only by the database poller."""

    def __init__(self, queue_size: int = 1000) -> None:
        self.queue_size = queue_size
        self.rooms: dict[str, set[asyncio.Queue[dict[str, Any] | None]]] = {}

    def subscribe(self, room: str) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(self.queue_size)
        self.rooms.setdefault(room, set()).add(q)
        return q

    def unsubscribe(self, room: str, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        subs = self.rooms.get(room)
        if subs is not None:
            subs.discard(q)
            if not subs:
                del self.rooms[room]

    def publish(self, room: str, message: dict[str, Any]) -> None:
        for q in list(self.rooms.get(room, ())):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client: tell its sender to close; the client reconnects with its last id.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    @property
    def connections(self) -> int:
        return sum(len(s) for s in self.rooms.values())


class Poller:
    def __init__(
        self,
        db: Any,
        hub: Hub,
        *,
        interval_s: float,
        grace_s: float,
        limit: int = 500,
        to_message: Callable[[dict[str, Any]], dict[str, Any]],
        on_poll: Callable[[float, bool], None] | None = None,
    ) -> None:
        self.db = db
        self.hub = hub
        self.interval_s = interval_s
        self.cursor = PollCursor(grace_s)
        self.limit = limit
        self.to_message = to_message
        self.on_poll = on_poll
        self.last_ok: float | None = None
        self.started = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: S110 - shutting down
                pass

    async def _init(self) -> None:
        ids = await self.db.recent_ids(self.limit, wait_s=30.0)
        self.cursor.start_from(ids)
        self.started = True
        log.info("poller started at watermark %d", self.cursor.watermark)

    async def poll_once(self) -> int:
        t0 = time.monotonic()
        ok = False
        try:
            rows = await self.db.poll(
                above=self.cursor.high, gaps=self.cursor.open_gaps(), limit=self.limit, wait_s=30.0
            )
            fresh = set(self.cursor.accept([int(r["id"]) for r in rows]))
            delivered = 0
            for row in rows:
                if int(row["id"]) in fresh:
                    fresh.discard(int(row["id"]))
                    msg = self.to_message(row)
                    self.hub.publish(msg["room"], msg)
                    delivered += 1
            self.last_ok = time.monotonic()
            ok = True
            return delivered
        finally:
            if self.on_poll is not None:
                self.on_poll((time.monotonic() - t0) * 1000.0, ok)

    async def _run(self) -> None:
        while not self.started:
            try:
                await self._init()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("poller init failed: %s", type(exc).__name__)
                await asyncio.sleep(1.0)
        while True:
            t0 = time.monotonic()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("poll failed: %s", type(exc).__name__)
            await asyncio.sleep(max(0.0, self.interval_s - (time.monotonic() - t0)))
