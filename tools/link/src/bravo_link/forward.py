"""Supervised ``kubectl port-forward`` (laptop -> cluster), the forward half of the temporary link.

    bravo-link forward --context bravo-source --namespace chat --resource svc/chat-db-rw \\
        --remote-port 5432 --local-port 15432 [--bind-address 127.0.0.1] \\
        [--probe pg|tcp] [--health-dsn-env SRC_DB_DSN] --status-file runs/link/src-db.json

* kubectl is restarted with backoff (1, 2, 4 ... 30 s) when it exits or when health fails
  ``--fail-threshold`` times in a row.
* Health is end to end, not just the local listener (kubectl accepts locally even when the
  remote side is gone): ``--probe pg`` sends a PostgreSQL SSLRequest and needs an 'S' or 'N'
  answer from the server; ``--health-dsn-env`` additionally runs ``SELECT 1`` with the DSN held
  in that environment variable (the value is never logged or written).
* Bind address: the default 127.0.0.1 is the safe choice. Lima's user-mode network maps
  ``host.lima.internal`` (192.168.5.2) to the host's loopback, so a K3s guest reaches a
  127.0.0.1 listener on the laptop (Lima docs, "Network": "the guest can access the host via
  host.lima.internal"). ``0.0.0.0`` would expose the DB on the LAN and is never the default.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass

from .status import LinkStatus, StatusWriter, utcnow

log = logging.getLogger("bravo_link.forward")

SSL_REQUEST = struct.pack("!ii", 8, 80877103)


def backoff_s(attempt: int, cap: float = 30.0) -> float:
    """1, 2, 4, ... seconds, capped."""
    return float(min(cap, 2 ** max(0, attempt - 1)))


@dataclass(frozen=True)
class ForwardSpec:
    context: str
    namespace: str
    resource: str
    remote_port: int
    local_port: int
    bind_address: str = "127.0.0.1"
    kubectl: str = "kubectl"

    def argv(self) -> list[str]:
        return [
            self.kubectl, "--context", self.context, "-n", self.namespace, "port-forward",
            "--address", self.bind_address, self.resource, f"{self.local_port}:{self.remote_port}",
        ]

    @property
    def connect_host(self) -> str:
        return "127.0.0.1" if self.bind_address in ("0.0.0.0", "localhost") else self.bind_address  # noqa: S104

    def describe(self) -> dict[str, object]:
        return {
            "context": self.context, "namespace": self.namespace, "resource": self.resource,
            "local": f"{self.bind_address}:{self.local_port}", "remote_port": self.remote_port,
        }


async def probe_tcp(host: str, port: int, timeout_s: float) -> None:
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def probe_pg(host: str, port: int, timeout_s: float) -> None:
    """SSLRequest: a live PostgreSQL (or PgBouncer) answers one byte, 'S' or 'N'."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
    try:
        writer.write(SSL_REQUEST)
        await writer.drain()
        answer = await asyncio.wait_for(reader.readexactly(1), timeout_s)
        if answer not in (b"S", b"N"):
            raise ConnectionError(f"unexpected SSLRequest answer {answer!r}")
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def probe_dsn(dsn: str, timeout_s: float) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=max(1, int(timeout_s))) as conn:
        cur = await conn.execute("SELECT 1")
        row = await cur.fetchone()
        if row is None or row[0] != 1:
            raise ConnectionError("SELECT 1 returned no row")


class PortForward:
    """One supervised kubectl port-forward. ``ready`` is set while health checks pass."""

    def __init__(
        self,
        spec: ForwardSpec,
        status: StatusWriter,
        name: str,
        probe: str = "tcp",
        health_dsn_env: str | None = None,
        interval_s: float = 5.0,
        timeout_s: float = 5.0,
        fail_threshold: int = 3,
    ) -> None:
        self.spec = spec
        self.writer = status
        self.status = LinkStatus(kind="forward", name=name, detail=spec.describe())
        self.probe = probe
        self.health_dsn_env = health_dsn_env
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.fail_threshold = fail_threshold
        self.ready = asyncio.Event()
        self.proc: asyncio.subprocess.Process | None = None

    def _set(self, state: str, error: str | None = None) -> None:
        self.status.state = state
        if error is not None:
            self.status.last_error = error[:300]
        if state == "up":
            self.status.last_ok = utcnow()
        self.writer.write(self.status)

    async def check(self) -> None:
        host, port = self.spec.connect_host, self.spec.local_port
        if self.probe == "pg":
            await probe_pg(host, port, self.timeout_s)
        else:
            await probe_tcp(host, port, self.timeout_s)
        if self.health_dsn_env:
            dsn = os.environ.get(self.health_dsn_env, "")
            if not dsn:
                raise ConnectionError(f"health DSN env {self.health_dsn_env} is empty")
            await probe_dsn(dsn, self.timeout_s)

    async def _start(self) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            *self.spec.argv(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        assert proc.stdout is not None
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), 30)
        except TimeoutError:
            line = b""
        if not line.startswith(b"Forwarding from"):
            err = b""
            if proc.returncode is None:
                proc.terminate()
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    err = await asyncio.wait_for(proc.stderr.read(), 5)
            raise ConnectionError(f"kubectl port-forward did not start: {(line + err).decode(errors='replace')[:300]}")
        asyncio.ensure_future(self._drain(proc))
        return proc

    @staticmethod
    async def _drain(proc: asyncio.subprocess.Process) -> None:
        """Consume kubectl output so its pipes never fill; per-connection errors are logged there."""
        async def one(stream: asyncio.StreamReader | None, level: int) -> None:
            if stream is None:
                return
            async for raw in stream:
                log.log(level, "kubectl: %s", raw.decode(errors="replace").rstrip()[:2000])

        await asyncio.gather(one(proc.stdout, logging.DEBUG), one(proc.stderr, logging.INFO))

    async def _stop_proc(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        self._set("starting")
        try:
            while not stop.is_set():
                try:
                    self.proc = await self._start()
                    self.status.detail["pid"] = self.proc.pid
                    await self._supervise(stop)
                except (ConnectionError, OSError) as exc:
                    self.ready.clear()
                    self._set("down", f"{type(exc).__name__}: {exc}")
                await self._stop_proc()
                if stop.is_set():
                    break
                attempt += 1
                self.status.restarts += 1
                self._set("down")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), backoff_s(attempt))
        finally:
            await self._stop_proc()
            self.ready.clear()
            self._set("stopped")

    async def _supervise(self, stop: asyncio.Event) -> None:
        failures = 0
        started = time.monotonic()
        while not stop.is_set():
            assert self.proc is not None
            if self.proc.returncode is not None:
                raise ConnectionError(f"kubectl exited with {self.proc.returncode}")
            try:
                await self.check()
                failures = 0
                self.ready.set()
                self._set("up")
            except (OSError, ConnectionError, TimeoutError, asyncio.IncompleteReadError, ImportError) as exc:
                failures += 1
                self.ready.clear()
                self._set("down", f"health: {type(exc).__name__}: {exc}")
                if failures >= self.fail_threshold:
                    raise ConnectionError(f"health failed {failures} times in a row") from exc
            except Exception as exc:  # psycopg errors from the DSN check
                failures += 1
                self.ready.clear()
                self._set("down", f"health: {type(exc).__name__}")
                if failures >= self.fail_threshold:
                    raise ConnectionError(f"health failed {failures} times in a row") from exc
            wait = self.interval_s if self.ready.is_set() or time.monotonic() - started > 10 else 0.5
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)


def free_local_port(bind: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((bind, 0))
        return int(s.getsockname()[1])
