"""Real-process fixtures: PostgreSQL containers, relay and agent subprocesses. No mocks.

Docker is required for the tunnel tests; they are skipped when `docker info` fails.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

PG_IMAGE = os.environ.get(
    "BRAVO_LINK_TEST_PG_IMAGE",
    "postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a",
)


def docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def container_reachable_host() -> str:
    """Host address a container reaches as ``host.docker.internal`` (containers get ``--add-host ...:host-gateway``).

    Docker Desktop forwards that name to the host's loopback, so a 127.0.0.1 listener is enough there. Docker
    Engine on Linux maps ``host-gateway`` to the default bridge gateway (docker0, e.g. 172.17.0.1): a local address
    of the host, not loopback, so a 127.0.0.1 listener cannot be reached from a container. Use that gateway when it
    is local.
    """
    out = subprocess.run(
        ["docker", "network", "inspect", "bridge", "--format", "{{range .IPAM.Config}}{{.Gateway}} {{end}}"],
        capture_output=True, text=True, check=False,
    ).stdout
    for gateway in out.split():
        with socket.socket() as s:
            try:
                s.bind((gateway, 0))
            except OSError:  # not an address of this host (Docker Desktop VM) or not IPv4
                continue
        return gateway
    return "127.0.0.1"


@dataclass
class PgContainer:
    name: str
    port: int
    password: str

    def dsn(self, host: str = "127.0.0.1", port: int | None = None) -> str:
        return (f"host={host} port={port or self.port} dbname=postgres user=postgres "
                f"password={self.password} sslmode=disable connect_timeout=10")


def start_pg(extra: list[str] | None = None) -> PgContainer:
    name = f"bravo-link-test-{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}",
         # Docker Desktop defines host.docker.internal itself; Docker Engine on Linux needs host-gateway.
         "--add-host", "host.docker.internal:host-gateway",
         "-p", "127.0.0.1::5432", PG_IMAGE, "postgres", "-c", "wal_level=logical",
         "-c", "wal_retrieve_retry_interval=1000", *(extra or [])],
        check=True, capture_output=True,
    )
    out = subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True).stdout
    port = int(out.strip().splitlines()[0].rsplit(":", 1)[1])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ok = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                            capture_output=True).returncode == 0
        if ok:
            return PgContainer(name, port, password)
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not become ready")


def stop_pg(pg: PgContainer) -> None:
    subprocess.run(["docker", "rm", "-f", pg.name], capture_output=True)


class Proc:
    """A bravo_link subprocess with its output captured to a file."""

    def __init__(self, args: list[str], log_path: Path) -> None:
        self.args = args
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> Proc:
        fh = self.log_path.open("ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "bravo_link", *self.args], stdout=fh, stderr=fh)
        return self

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def relay_stats(agent_port: int) -> dict[str, object]:
    out = subprocess.run([sys.executable, "-m", "bravo_link", "probe-relay", "--relay-addr", f"127.0.0.1:{agent_port}"],
                         capture_output=True, text=True, timeout=15)
    return dict(json.loads(out.stdout))


def wait_idle(agent_port: int, want: int, timeout_s: float = 20) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if int(str(relay_stats(agent_port)["idle_agents"])) >= want:
                return
        except Exception:  # noqa: BLE001, S110 - relay still starting
            pass
        time.sleep(0.2)
    raise TimeoutError(f"relay did not get {want} idle agent connections")


@pytest.fixture
def docker_required() -> None:
    if not docker_ok():
        pytest.skip("docker is not available")


@pytest.fixture
def publisher(docker_required: None) -> Iterator[PgContainer]:
    pg = start_pg()
    try:
        yield pg
    finally:
        stop_pg(pg)


@pytest.fixture
def subscriber(docker_required: None) -> Iterator[PgContainer]:
    pg = start_pg()
    try:
        yield pg
    finally:
        stop_pg(pg)


@dataclass
class Tunnel:
    client_host: str  # relay client listener: reachable from the host and, as host.docker.internal, from containers
    client_port: int
    agent_port: int
    relay: Proc
    agent: Proc
    status_file: Path

    def restart_agent(self) -> None:
        self.agent.start()
        wait_idle(self.agent_port, 4)


@pytest.fixture
def tunnel(publisher: PgContainer, tmp_path: Path) -> Iterator[Tunnel]:
    client_host = container_reachable_host()
    client_port, agent_port = free_port(client_host), free_port()
    relay = Proc(["relay", "--client-bind", client_host, "--client-port", str(client_port),
                  "--agent-port", str(agent_port), "--wait-agent-timeout", "3", "--stats-interval", "5"],
                 tmp_path / "relay.log").start()
    status = tmp_path / "reverse.json"
    agent = Proc(["reverse", "--relay-addr", f"127.0.0.1:{agent_port}", "--target", f"127.0.0.1:{publisher.port}",
                  "--pool", "4", "--ping-interval", "2", "--status-file", str(status)],
                 tmp_path / "agent.log").start()
    try:
        wait_idle(agent_port, 4)
        yield Tunnel(client_host, client_port, agent_port, relay, agent, status)
    finally:
        agent.stop()
        relay.stop()
