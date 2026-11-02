"""Rendezvous relay for the reverse tunnel. Runs in the source cluster (EKS) pod.

Standard library only and Python >= 3.9, so it runs from a ConfigMap on any image that has
``python3`` (``python3 relay.py --help``). It is also ``python -m bravo_link relay``.

Two listeners:

* client port (Service ``dst-db-link``): in-cluster clients such as the reverse logical
  replication subscription on the source DB.
* agent port, bound to 127.0.0.1 only: reached solely through ``kubectl port-forward`` from the
  laptop (the CRI dials localhost inside the pod network namespace). Nothing in the cluster
  network can reach it, so the agent port needs no token.

Protocol on an agent connection (one line each, then raw bytes):

    agent -> relay   BRAVO-LINK/1 READY        register an idle connection
    agent -> relay   PING                      keepalive while idle (ignored)
    relay -> agent   OPEN                      a client is waiting: connect to your target
    agent -> relay   OK | ERR <reason>         target connected (or not); after OK both sides
                                               splice bytes until close
    agent -> relay   BRAVO-LINK/1 PROBE        health probe: relay answers one JSON stats line

The relay pairs each client with one idle agent connection. An idle connection that is dead
(EOF, no OK within --open-timeout) is dropped and the next one is tried. A client waits up to
--wait-agent-timeout for an idle connection, then is closed.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import signal
import sys
import time
from typing import Deque, Dict, Optional, Tuple

HELLO = b"BRAVO-LINK/1 READY\n"
PROBE = b"BRAVO-LINK/1 PROBE\n"
PING = b"PING\n"
OPEN = b"OPEN\n"
OK = b"OK\n"
CHUNK = 65536
LINE_LIMIT = 1024

log = logging.getLogger("bravo_link.relay")


class AgentConn:
    __slots__ = ("reader", "writer", "watch", "dead", "since")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.watch: Optional[asyncio.Task] = None
        self.dead = False
        self.since = time.monotonic()


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:  # noqa: BLE001, S110 - the peer may already be gone
        pass


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, counter: Dict[str, int], key: str) -> None:
    """Copy until EOF, honouring backpressure (drain). Half-closes the peer on EOF."""
    while True:
        data = await reader.read(CHUNK)
        if not data:
            break
        writer.write(data)
        await writer.drain()
        counter[key] += len(data)
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, RuntimeError):
        pass


async def splice(
    a: Tuple[asyncio.StreamReader, asyncio.StreamWriter],
    b: Tuple[asyncio.StreamReader, asyncio.StreamWriter],
    counter: Dict[str, int],
    half_close_timeout: float,
) -> None:
    """Bidirectional copy a<->b. After one direction ends, the other gets half_close_timeout."""
    up = asyncio.ensure_future(pipe(a[0], b[1], counter, "bytes_up"))
    down = asyncio.ensure_future(pipe(b[0], a[1], counter, "bytes_down"))
    try:
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        failed = any(t.exception() is not None for t in done if not t.cancelled())
        if pending and not failed:
            await asyncio.wait(pending, timeout=half_close_timeout)
    finally:
        for t in (up, down):
            if not t.done():
                t.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        _close(a[1])
        _close(b[1])


class Relay:
    def __init__(self, open_timeout: float, wait_agent_timeout: float, half_close_timeout: float) -> None:
        self.open_timeout = open_timeout
        self.wait_agent_timeout = wait_agent_timeout
        self.half_close_timeout = half_close_timeout
        self.idle: Deque[AgentConn] = collections.deque()
        self.changed = asyncio.Event()
        self.started = time.time()
        self.stats: Dict[str, int] = {
            "clients_total": 0,
            "clients_paired": 0,
            "clients_rejected_no_agent": 0,
            "clients_rejected_target": 0,
            "agents_registered": 0,
            "agents_dead_on_open": 0,
            "active_pairs": 0,
            "bytes_up": 0,
            "bytes_down": 0,
        }

    def snapshot(self) -> Dict[str, object]:
        out: Dict[str, object] = dict(self.stats)
        out["idle_agents"] = len(self.idle)
        out["uptime_s"] = round(time.time() - self.started, 1)
        return out

    def _notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    async def on_agent(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            _close(writer)
            return
        if line == PROBE:
            writer.write(json.dumps(self.snapshot()).encode() + b"\n")
            try:
                await writer.drain()
            finally:
                _close(writer)
            return
        if line != HELLO:
            _close(writer)
            return
        conn = AgentConn(reader, writer)
        conn.watch = asyncio.ensure_future(self._watch(conn))
        self.idle.append(conn)
        self.stats["agents_registered"] += 1
        self._notify()

    async def _watch(self, conn: AgentConn) -> None:
        """While idle: consume PING lines, mark the connection dead on EOF or error."""
        try:
            while True:
                line = await conn.reader.readline()
                if not line:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001, S110 - any read error means the idle connection is dead
            pass
        conn.dead = True
        try:
            self.idle.remove(conn)
        except ValueError:
            pass
        _close(conn.writer)

    async def _take(self, deadline: float) -> Optional[AgentConn]:
        while True:
            while self.idle:
                conn = self.idle.popleft()
                if conn.watch is not None and not conn.watch.done():
                    conn.watch.cancel()
                    await asyncio.gather(conn.watch, return_exceptions=True)
                if not conn.dead:
                    return conn
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            waiter = self.changed
            try:
                await asyncio.wait_for(waiter.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    async def _open(self, conn: AgentConn) -> bytes:
        conn.writer.write(OPEN)
        await conn.writer.drain()
        while True:
            line = await conn.reader.readline()
            if not line:
                raise ConnectionError("agent closed before OK")
            if line == PING:
                continue
            return line

    async def on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.stats["clients_total"] += 1
        deadline = time.monotonic() + self.wait_agent_timeout
        while True:
            conn = await self._take(deadline)
            if conn is None:
                self.stats["clients_rejected_no_agent"] += 1
                log.warning("client closed: no idle agent connection within %.1fs", self.wait_agent_timeout)
                _close(writer)
                return
            try:
                ack = await asyncio.wait_for(self._open(conn), self.open_timeout)
            except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
                self.stats["agents_dead_on_open"] += 1
                _close(conn.writer)
                continue
            if ack == OK:
                break
            self.stats["clients_rejected_target"] += 1
            log.warning("agent could not reach target: %s", ack.decode(errors="replace").strip()[:200])
            _close(conn.writer)
            _close(writer)
            return
        self.stats["clients_paired"] += 1
        self.stats["active_pairs"] += 1
        try:
            await splice((reader, writer), (conn.reader, conn.writer), self.stats, self.half_close_timeout)
        finally:
            self.stats["active_pairs"] -= 1


async def serve(args: argparse.Namespace) -> None:
    relay = Relay(args.open_timeout, args.wait_agent_timeout, args.half_close_timeout)
    agents = await asyncio.start_server(relay.on_agent, args.agent_bind, args.agent_port, limit=LINE_LIMIT * 64)
    clients = await asyncio.start_server(relay.on_client, args.client_bind, args.client_port)
    log.info(
        json.dumps(
            {"event": "relay_listening", "client": f"{args.client_bind}:{args.client_port}",
             "agent": f"{args.agent_bind}:{args.agent_port}"}
        )
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    async def report() -> None:
        while True:
            await asyncio.sleep(args.stats_interval)
            log.info(json.dumps({"event": "relay_stats", **relay.snapshot()}))

    reporter = asyncio.ensure_future(report())
    await stop.wait()
    reporter.cancel()
    agents.close()
    clients.close()
    await agents.wait_closed()
    await clients.wait_closed()


def build_parser(parser: Optional[argparse.ArgumentParser] = None) -> argparse.ArgumentParser:
    p = parser or argparse.ArgumentParser(prog="relay.py", description=__doc__,
                                          formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-bind", default="0.0.0.0", help="address for in-cluster clients (default 0.0.0.0)")  # noqa: S104
    p.add_argument("--client-port", type=int, required=True)
    p.add_argument("--agent-bind", default="127.0.0.1",
                   help="address for agent connections; keep 127.0.0.1 (reached via port-forward only)")
    p.add_argument("--agent-port", type=int, required=True)
    p.add_argument("--open-timeout", type=float, default=5.0, help="seconds for an agent to answer OPEN")
    p.add_argument("--wait-agent-timeout", type=float, default=15.0, help="seconds a client waits for an agent")
    p.add_argument("--half-close-timeout", type=float, default=60.0)
    p.add_argument("--stats-interval", type=float, default=30.0)
    return p


def main(argv: Optional[list] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
