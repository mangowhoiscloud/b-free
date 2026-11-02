"""Laptop agent of the reverse tunnel (``bravo-link reverse``).

Keeps ``--pool`` idle connections open to the relay's agent port and splices each one the
relay pairs with an in-cluster client to ``--target`` (a host:port reachable from the laptop,
for example a K3s port-forward). Protocol: relay.py docstring.

    # relay agent port through a supervised port-forward, K3s DB through another one:
    bravo-link reverse \\
        --relay-context bravo-source --relay-namespace chat --relay-resource deploy/dst-db-link \\
        --relay-agent-port 7070 --relay-local-port 17070 \\
        --target-context <k3s> --target-namespace chat --target-resource svc/chat-db-rw \\
        --target-remote-port 5432 --target-local-port 25432 \\
        --pool 8 --status-file runs/link/reverse.json

    # or plain addresses (tests, an already running forward):
    bravo-link reverse --relay-addr 127.0.0.1:17070 --target 127.0.0.1:25432

Every slot reconnects with backoff when the relay or the port-forward goes away. A slot that
is paired hands its connection to a splice task and immediately opens a new idle connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from .relay import HELLO, OK, OPEN, PING, PROBE, splice
from .status import LinkStatus, StatusWriter, utcnow

log = logging.getLogger("bravo_link.agent")


def parse_addr(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {value!r}")
    return host, int(port)


@dataclass
class AgentStats:
    paired: int = 0
    target_failures: int = 0
    relay_failures: int = 0
    active: int = 0
    counters: dict[str, int] = field(default_factory=lambda: {"bytes_up": 0, "bytes_down": 0})


class ReverseAgent:
    def __init__(
        self,
        relay: tuple[str, int],
        target: tuple[str, int],
        pool: int,
        status: StatusWriter,
        name: str = "reverse",
        ping_interval_s: float = 10.0,
        connect_timeout_s: float = 5.0,
        half_close_timeout_s: float = 60.0,
    ) -> None:
        self.relay = relay
        self.target = target
        self.pool = pool
        self.ping_interval_s = ping_interval_s
        self.connect_timeout_s = connect_timeout_s
        self.half_close_timeout_s = half_close_timeout_s
        self.stats = AgentStats()
        self.writer = status
        self.status = LinkStatus(
            kind="reverse", name=name,
            detail={"relay": f"{relay[0]}:{relay[1]}", "target": f"{target[0]}:{target[1]}", "pool": pool},
        )
        self.idle = 0
        self._splices: set[asyncio.Task[None]] = set()

    def _publish(self, state: str | None = None, error: str | None = None) -> None:
        if state is not None:
            self.status.state = state
        if error is not None:
            self.status.last_error = error[:300]
        self.status.detail.update(
            {"idle": self.idle, "active": self.stats.active, "paired": self.stats.paired,
             "target_failures": self.stats.target_failures, "relay_failures": self.stats.relay_failures,
             **self.stats.counters}
        )
        self.writer.write(self.status)

    async def _wait_open(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), self.ping_interval_s)
            except TimeoutError:
                writer.write(PING)
                await writer.drain()
                continue
            if not line:
                raise ConnectionError("relay closed the idle connection")
            if line == OPEN:
                return

    async def _slot(self, stop: asyncio.Event, slot: int) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(*self.relay), self.connect_timeout_s)
                writer.write(HELLO)
                await writer.drain()
            except (OSError, TimeoutError) as exc:
                attempt += 1
                self.stats.relay_failures += 1
                self._publish("down", f"relay: {type(exc).__name__}: {exc}")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), min(5.0, 0.2 * 2**attempt))
                continue
            attempt = 0
            self.idle += 1
            self.status.last_ok = utcnow()
            self._publish("up")
            try:
                await self._wait_open(reader, writer)
            except (OSError, ConnectionError, ValueError) as exc:
                self.idle -= 1
                writer.close()
                self._publish(None, f"idle connection lost: {type(exc).__name__}")
                await asyncio.sleep(0.2)
                continue
            self.idle -= 1
            try:
                t_reader, t_writer = await asyncio.wait_for(
                    asyncio.open_connection(*self.target), self.connect_timeout_s
                )
            except (OSError, TimeoutError) as exc:
                self.stats.target_failures += 1
                with contextlib.suppress(Exception):
                    writer.write(f"ERR target {type(exc).__name__}: {exc}\n".encode()[:500])
                    await writer.drain()
                writer.close()
                self._publish(None, f"target: {type(exc).__name__}: {exc}")
                continue
            writer.write(OK)
            await writer.drain()
            self.stats.paired += 1
            task = asyncio.ensure_future(self._run_splice((reader, writer), (t_reader, t_writer)))
            self._splices.add(task)
            task.add_done_callback(self._splices.discard)
            self._publish()

    async def _run_splice(
        self,
        relay_side: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        target_side: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ) -> None:
        self.stats.active += 1
        try:
            # relay -> target is "up" (client in the cluster towards the K3s side).
            await splice(relay_side, target_side, self.stats.counters, self.half_close_timeout_s)
        except Exception as exc:  # noqa: BLE001 - one connection fails alone
            log.info("splice ended: %s", type(exc).__name__)
        finally:
            self.stats.active -= 1
            self._publish()

    async def run(self, stop: asyncio.Event) -> None:
        self._publish("starting")
        slots = [asyncio.ensure_future(self._slot(stop, i)) for i in range(self.pool)]

        async def heartbeat() -> None:
            while not stop.is_set():
                self._publish()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 5)

        hb = asyncio.ensure_future(heartbeat())
        try:
            await stop.wait()
        finally:
            for t in [*slots, hb, *self._splices]:
                t.cancel()
            await asyncio.gather(*slots, hb, *self._splices, return_exceptions=True)
            self.idle = 0
            self._publish("stopped")


async def probe_relay(addr: tuple[str, int], timeout_s: float = 5.0) -> dict[str, object]:
    """Stats from the relay (through its agent port)."""
    import json

    reader, writer = await asyncio.wait_for(asyncio.open_connection(*addr), timeout_s)
    try:
        writer.write(PROBE)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout_s)
        return dict(json.loads(line))
    finally:
        writer.close()


def monotonic_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000
