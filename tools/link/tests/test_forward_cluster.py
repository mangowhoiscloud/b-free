"""Forward supervisor against a real cluster: kubectl port-forward to kube-dns (TCP 53).

Uses BRAVO_LINK_TEST_CONTEXT (default bravo-source). Read-only: no cluster objects are created.
Proves end-to-end health, a JSON status file, and restart after the kubectl process dies.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import struct
import subprocess
from pathlib import Path

import pytest

from bravo_link.forward import ForwardSpec, PortForward, free_local_port
from bravo_link.status import StatusWriter

CONTEXT = os.environ.get("BRAVO_LINK_TEST_CONTEXT", "bravo-source")


def _cluster_ok() -> bool:
    if shutil.which("kubectl") is None:
        return False
    r = subprocess.run(["kubectl", "--context", CONTEXT, "-n", "kube-system", "get", "svc", "kube-dns"],
                       capture_output=True, timeout=60)
    return r.returncode == 0


def _dns_query(name: str) -> bytes:
    q = struct.pack("!HHHHHH", 0x4242, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    q += struct.pack("!HH", 1, 1)
    return struct.pack("!H", len(q)) + q


async def _resolve_over_tcp(port: int, name: str) -> int:
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
    try:
        writer.write(_dns_query(name))
        await writer.drain()
        (length,) = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 10))
        resp = await asyncio.wait_for(reader.readexactly(length), 10)
    finally:
        writer.close()
    _id, flags, _qd, an, _ns, _ar = struct.unpack("!HHHHHH", resp[:12])
    assert _id == 0x4242 and flags & 0x000F == 0, f"rcode {flags & 0xF}"
    return an


async def test_forward_supervisor_restarts_kubectl(tmp_path: Path) -> None:
    if not _cluster_ok():
        pytest.skip(f"kubectl context {CONTEXT} or kube-dns not reachable")
    port = free_local_port()
    status_path = tmp_path / "dns.json"
    pf = PortForward(ForwardSpec(CONTEXT, "kube-system", "svc/kube-dns", 53, port), StatusWriter(status_path),
                     "kube-dns", probe="tcp", interval_s=1.0)
    stop = asyncio.Event()
    task = asyncio.ensure_future(pf.run(stop))
    try:
        await asyncio.wait_for(pf.ready.wait(), 60)
        assert await _resolve_over_tcp(port, "kubernetes.default.svc.cluster.local") >= 1
        first_pid = pf.proc.pid if pf.proc else None
        assert first_pid is not None
        os.kill(first_pid, signal.SIGKILL)  # the port-forward dies under the supervisor
        for _ in range(120):
            await asyncio.sleep(0.5)
            if pf.status.restarts >= 1 and pf.ready.is_set() and pf.proc and pf.proc.pid != first_pid:
                break
        assert pf.status.restarts >= 1 and pf.ready.is_set()
        assert await _resolve_over_tcp(port, "kubernetes.default.svc.cluster.local") >= 1
        status = json.loads(status_path.read_text())
        assert status["label"] == "임시 링크(D-16 전)" and status["state"] == "up" and status["last_ok"]
        print(json.dumps({"restarts": status["restarts"], "state": status["state"], "detail": status["detail"]}))
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
    assert json.loads(status_path.read_text())["state"] == "stopped"
