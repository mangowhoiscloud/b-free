"""PgBouncer pools the Executor pauses, retargets and resumes (03-migration.md 4.1, 4.2 steps 3 and 7).

A pool is one PgBouncer: its admin console, the app path through it (for the step-8 probe) and
a target switch. Two switch kinds:

* ``LocalIniSwitch`` (Compose pilot): rewrite ``[databases]`` in a pgbouncer.ini on a shared volume.
* ``K8sExecSwitch`` (clusters, CLOUD-E2E-CONTRACT.md 9): ``pods/exec`` into the PgBouncer Pod and
  atomically replace the active file ``/run/pgbouncer/active.ini`` (emptyDir) with one of the two
  target files mounted from a ConfigMap directory (``target-src.ini``, ``target-dst.ini``):
  copy to a temp file in the same directory, then ``mv``. RELOAD is sent by the runner.

Which side a pool currently serves is read from the real state (file content), never assumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .pgbouncer import read_database_target, rewrite_database_target, write_ini_atomically

_HOST_RE = re.compile(r"(?:^|\s)host=(\S+)")
_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:@-]+$")


def host_of(target: str | None) -> str | None:
    if not target:
        return None
    m = _HOST_RE.search(target)
    return None if m is None else m.group(1)


class SwitchError(RuntimeError):
    """The target switch could not read or change the pool's active target."""


class TargetSwitch(Protocol):
    async def current_side(self) -> str | None: ...
    async def target_line(self, side: str) -> str | None: ...
    async def preflight(self) -> dict[str, Any]: ...
    async def activate(self, side: str) -> dict[str, Any]: ...
    def describe(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LocalIniSwitch:
    path: Path
    db_name: str
    targets: dict[str, str]  # side -> connect string of the [databases] entry

    async def current_side(self) -> str | None:
        now = read_database_target(self.path.read_text(encoding="utf-8"), self.db_name)
        return next((side for side, t in self.targets.items() if t == now), None)

    async def target_line(self, side: str) -> str | None:
        return self.targets.get(side)

    async def preflight(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8")
        return {"ini": str(self.path), "entry_present": read_database_target(text, self.db_name) is not None}

    async def activate(self, side: str) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8")
        before = read_database_target(text, self.db_name)
        write_ini_atomically(self.path, rewrite_database_target(text, self.db_name, self.targets[side]))
        return {"from": before, "to": self.targets[side]}

    def describe(self) -> dict[str, Any]:
        return {"kind": "local-ini", "path": str(self.path)}


@dataclass(slots=True)
class K8sExecSwitch:
    """``kubectl exec`` into the PgBouncer workload. Needs only ``pods/exec`` on that Pod."""

    context: str
    namespace: str
    workload: str  # e.g. deploy/chat-pgbouncer or pod/chat-pgbouncer-0
    db_name: str
    targets_dir: str = "/etc/pgbouncer/targets"
    active_path: str = "/run/pgbouncer/active.ini"
    container: str | None = None
    timeout_s: float = 20.0
    kubectl: str = "kubectl"
    _cache: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for v in (self.context, self.namespace, self.workload, self.targets_dir, self.active_path):
            if not _SAFE_RE.match(v):
                raise SwitchError(f"unsafe k8s switch parameter: {v!r}")

    def _target_file(self, side: str) -> str:
        if side not in ("src", "dst"):
            raise SwitchError(f"unknown side {side!r}")
        return f"{self.targets_dir}/target-{side}.ini"

    async def _exec(self, script: str) -> str:
        cmd = [self.kubectl, "--context", self.context, "-n", self.namespace, "exec", self.workload]
        if self.container:
            cmd += ["-c", self.container]
        cmd += ["--", "sh", "-c", script]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError as exc:
            proc.kill()
            raise SwitchError(f"kubectl exec timed out after {self.timeout_s}s") from exc
        if proc.returncode != 0:
            raise SwitchError(f"kubectl exec exit {proc.returncode}: {err.decode(errors='replace')[-300:]}")
        return out.decode()

    async def _read(self, path: str) -> str:
        return await self._exec(f"cat {path}")

    async def target_line(self, side: str) -> str | None:
        if side not in self._cache:
            self._cache[side] = await self._read(self._target_file(side))
        return read_database_target(self._cache[side], self.db_name)

    async def current_side(self) -> str | None:
        active = await self._read(self.active_path)
        digest = hashlib.sha256(active.encode()).hexdigest()
        for side in ("src", "dst"):
            if side not in self._cache:
                self._cache[side] = await self._read(self._target_file(side))
            if hashlib.sha256(self._cache[side].encode()).hexdigest() == digest:
                return side
        return None

    async def preflight(self) -> dict[str, Any]:
        out = await self._exec(
            f"hostname; ls {self.targets_dir}/target-src.ini {self.targets_dir}/target-dst.ini {self.active_path}"
        )
        lines = out.split()
        return {"pod": lines[0] if lines else None, "files": lines[1:], "workload": self.workload,
                "context": self.context}

    async def activate(self, side: str) -> dict[str, Any]:
        src = self._target_file(side)
        tmp = f"{self.active_path}.tmp"
        out = await self._exec(
            f"set -e; hostname; cp {src} {tmp}; mv -f {tmp} {self.active_path}; sha256sum {self.active_path}"
        )
        lines = out.split("\n")
        return {"pod": lines[0].strip(), "active_sha256": (lines[1].split() or [""])[0], "from_file": src}

    def describe(self) -> dict[str, Any]:
        return {"kind": "k8s-exec", "context": self.context, "namespace": self.namespace, "workload": self.workload,
                "active_path": self.active_path}


@dataclass(frozen=True, slots=True)
class PoolSpec:
    name: str  # "0" (Compose) or the cluster side the PgBouncer runs in ("src", "dst")
    admin_dsn: str
    app_dsn: str
    db_name: str
    switch: TargetSwitch

    @property
    def resource(self) -> str:
        return f"pgbouncer/{self.name}"

    @property
    def active_resource(self) -> str:
        return f"pgbouncer/{self.name}/active"

    @property
    def path_resource(self) -> str:
        return f"pgbouncer/{self.name}/db:{self.db_name}"
