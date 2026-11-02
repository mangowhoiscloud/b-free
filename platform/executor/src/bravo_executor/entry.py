"""Entry-point write block (spec r2 §8.10, CLOUD-E2E-CONTRACT.md 8): the demo app's admin switch.

``GET /admin/writes`` -> ``{"blocked": bool, "since": ts}``; ``PUT /admin/writes {"blocked": b}``
returns once writes in flight have ended. The state is a row per app environment (``env_id``) in
the database the app's writes reach, so

* cutover/failback step 2 blocks every entry (each app environment) on the origin DB;
* "open writes" at the end unblocks the entries on the target DB (after a failback the source DB
  still holds the rows the cutover blocked);
* ABORT_PRE_WRITE unblocks only the entries this run blocked.

The bearer token is never logged or returned.
"""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass
from typing import Any

import httpx

# k8s-pf://<context>/<namespace>/<label selector>/<port>: reach the admin port through a kubectl
# port-forward to one Running pod (RBAC pods/portforward, CLOUD-E2E-CONTRACT.md 9) when no admin
# link Service exists. The port-forward lives only for one call.
_PF_RE = re.compile(r"^k8s-pf://(?P<ctx>[^/]+)/(?P<ns>[^/]+)/(?P<sel>[A-Za-z0-9_./=,-]+)/(?P<port>[0-9]+)$")


class EntryError(RuntimeError):
    """The admin switch did not answer as the contract says."""


@dataclass(frozen=True, slots=True)
class EntrySpec:
    name: str  # app environment id (env_id), e.g. aws, onprem
    side: str  # cluster the app instance runs in
    url: str   # admin base URL reachable from the Executor (port-forward of Service chat-admin:8081)
    token: str

    @property
    def resource(self) -> str:
        return f"entry/{self.name}/write-block"

    def __repr__(self) -> str:  # never show the token
        return f"EntrySpec(name={self.name!r}, side={self.side!r}, url={self.url!r})"


class _PortForward:
    def __init__(self, url: str, timeout_s: float) -> None:
        m = _PF_RE.match(url)
        if m is None:
            raise EntryError(f"bad k8s-pf url {url!r}")
        self._m = m
        self._timeout = timeout_s
        self._proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> str:
        ctx, ns, sel, port = self._m["ctx"], self._m["ns"], self._m["sel"], self._m["port"]
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "--context", ctx, "-n", ns, "get", "pods", "-l", sel,
            "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), self._timeout)
        pod = out.decode().strip()
        if proc.returncode != 0 or not pod:
            raise EntryError(f"no running pod for {sel} in {ctx}/{ns}: {err.decode(errors='replace')[-200:]}")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local = s.getsockname()[1]
        self._proc = await asyncio.create_subprocess_exec(
            "kubectl", "--context", ctx, "-n", ns, "port-forward", f"pod/{pod}", f"{local}:{port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert self._proc.stdout is not None
        line = await asyncio.wait_for(self._proc.stdout.readline(), self._timeout)  # "Forwarding from ..."
        if not line.startswith(b"Forwarding from"):
            raise EntryError(f"port-forward to {pod} did not start")
        return f"http://127.0.0.1:{local}"

    async def __aexit__(self, *exc: object) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()


class WriteBlockClient:
    def __init__(self, spec: EntrySpec, timeout_s: float = 30.0) -> None:
        self.spec = spec
        self._timeout = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.spec.token}"}

    async def _request(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.spec.url.startswith("k8s-pf://"):
            async with _PortForward(self.spec.url, self._timeout) as base:
                return await self._http(base, method, body)
        return await self._http(self.spec.url, method, body)

    async def _http(self, base: str, method: str, body: dict[str, Any] | None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            r = await http.request(method, f"{base}/admin/writes", headers=self._headers(), json=body)
        return self._parse(r)

    async def get(self) -> dict[str, Any]:
        return await self._request("GET")

    async def set(self, blocked: bool) -> dict[str, Any]:
        return await self._request("PUT", {"blocked": blocked})

    def _parse(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code != 200:
            raise EntryError(f"{self.spec.name}: HTTP {r.status_code} from /admin/writes")
        doc = r.json()
        if not isinstance(doc, dict) or not isinstance(doc.get("blocked"), bool):
            raise EntryError(f"{self.spec.name}: unexpected body from /admin/writes")
        return {"blocked": doc["blocked"], "since": doc.get("since")}
