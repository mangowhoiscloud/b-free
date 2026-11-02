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

from dataclasses import dataclass
from typing import Any

import httpx


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


class WriteBlockClient:
    def __init__(self, spec: EntrySpec, timeout_s: float = 15.0) -> None:
        self.spec = spec
        self._timeout = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.spec.token}"}

    async def get(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            r = await http.get(f"{self.spec.url}/admin/writes", headers=self._headers())
        return self._parse(r)

    async def set(self, blocked: bool) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            r = await http.put(f"{self.spec.url}/admin/writes", headers=self._headers(), json={"blocked": blocked})
        return self._parse(r)

    def _parse(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code != 200:
            raise EntryError(f"{self.spec.name}: HTTP {r.status_code} from /admin/writes")
        doc = r.json()
        if not isinstance(doc, dict) or not isinstance(doc.get("blocked"), bool):
            raise EntryError(f"{self.spec.name}: unexpected body from /admin/writes")
        return {"blocked": doc["blocked"], "since": doc.get("since")}
