"""Platform OpenBao access for the Executor (D-26).

The Executor holds no long-lived secret. ``deploy/pilot/scripts/executor-bao.sh with-executor``
mints one AppRole secret_id (60 s, 1 use) and starts the Executor with

    BRAVO_BAO_ADDR, BRAVO_BAO_CACERT, BRAVO_BAO_ROLE_ID, BRAVO_BAO_SECRET_ID

``SecretBroker`` logs in once (policy ``platform-executor``: read-only on the Cloudflare secret and
``secret/envs/<env>/{chat-admin,chat-db,object-store}``), keeps values in memory for the run and
revokes its token on ``close()``. Errors and reprs carry paths and HTTP status only, never values.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx


class VaultError(RuntimeError):
    """An OpenBao call failed. The message holds the path and status, never a secret."""

    def __init__(self, op: str, path: str, status: int | None, detail: str = "") -> None:
        super().__init__(f"OpenBao {op} {path}: HTTP {status}{(' ' + detail) if detail else ''}")
        self.op = op
        self.path = path
        self.status = status


class Forbidden(VaultError):
    """HTTP 403: the token's policy does not cover the path."""


@dataclass(frozen=True, slots=True)
class VaultConfig:
    addr: str
    cacert: str | None
    role_id: str = field(repr=False)
    secret_id: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VaultConfig:
        e = os.environ if env is None else env
        missing = [k for k in ("BRAVO_BAO_ADDR", "BRAVO_BAO_ROLE_ID", "BRAVO_BAO_SECRET_ID") if not e.get(k)]
        if missing:
            raise VaultError("config", ",".join(missing), None, "missing (run via executor-bao.sh with-executor)")
        return cls(
            addr=e["BRAVO_BAO_ADDR"].rstrip("/"),
            cacert=e.get("BRAVO_BAO_CACERT") or None,
            role_id=e["BRAVO_BAO_ROLE_ID"],
            secret_id=e["BRAVO_BAO_SECRET_ID"],
        )


class VaultClient:
    """Minimal OpenBao HTTP client: AppRole login, KV v2 read, revoke-self."""

    def __init__(self, addr: str, cacert: str | None, timeout_s: float = 10.0) -> None:
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=cacert) if cacert else True
        self._http = httpx.AsyncClient(base_url=f"{addr}/v1/", verify=verify, timeout=timeout_s)
        self._token: str | None = None

    def __repr__(self) -> str:
        return f"VaultClient(base={self._http.base_url!s}, logged_in={self._token is not None})"

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self._token} if self._token else {}

    async def _call(self, op: str, method: str, path: str, json: Any | None = None) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, path, json=json, headers=self._headers())
        except httpx.HTTPError as exc:
            raise VaultError(op, path, None, type(exc).__name__) from None
        if resp.status_code == 403:
            raise Forbidden(op, path, 403)
        if resp.status_code >= 300:
            raise VaultError(op, path, resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def approle_login(self, role_id: str, secret_id: str) -> dict[str, Any]:
        """Returns non-secret token facts (policies, ttl, accessor)."""
        body = await self._call(
            "login", "POST", "auth/approle/login", json={"role_id": role_id, "secret_id": secret_id}
        )
        auth = body.get("auth") or {}
        token = auth.get("client_token")
        if not token:
            raise VaultError("login", "auth/approle/login", None, "no client_token in response")
        self._token = token
        return {
            "policies": auth.get("token_policies") or auth.get("policies"),
            "lease_duration_s": auth.get("lease_duration"),
            "accessor": auth.get("accessor"),
        }

    async def kv_read(self, path: str) -> dict[str, Any]:
        """KV v2 read of ``secret/<path>``; returns the data map."""
        body = await self._call("read", "GET", f"secret/data/{path}")
        data = (body.get("data") or {}).get("data")
        if not isinstance(data, dict):
            raise VaultError("read", f"secret/data/{path}", None, "no data")
        return data

    async def revoke_self(self) -> None:
        if self._token is None:
            return
        try:
            await self._call("revoke", "POST", "auth/token/revoke-self")
        finally:
            self._token = None

    async def close(self) -> None:
        try:
            await self.revoke_self()
        finally:
            await self._http.aclose()


class SecretBroker:
    """One login per run, in-memory cache, token revoked on close()."""

    def __init__(self, cfg: VaultConfig) -> None:
        self._cfg = cfg
        self._client = VaultClient(cfg.addr, cfg.cacert)
        self._cache: dict[str, dict[str, Any]] = {}
        self.login_facts: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return f"SecretBroker(addr={self._cfg.addr}, cached_paths={sorted(self._cache)})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SecretBroker:
        return cls(VaultConfig.from_env(env))

    async def _ensure_login(self) -> None:
        if not self._client.logged_in:
            # secret_id is single-use: a second login in the same run fails by design.
            self.login_facts = await self._client.approle_login(self._cfg.role_id, self._cfg.secret_id)

    async def read(self, path: str) -> dict[str, Any]:
        """Values of ``secret/<path>`` (KV v2), cached for the run."""
        if path not in self._cache:
            await self._ensure_login()
            self._cache[path] = await self._client.kv_read(path)
        return self._cache[path]

    async def field(self, path: str, name: str) -> str:
        data = await self.read(path)
        value = data.get(name)
        if not isinstance(value, str) or not value:
            raise VaultError("read", f"secret/data/{path}", None, f"field {name!r} missing or empty")
        return value

    async def field_names(self, path: str) -> dict[str, str]:
        """Evidence form: field -> "set" | "empty". Never the values."""
        data = await self.read(path)
        return {k: ("set" if v not in (None, "") else "empty") for k, v in sorted(data.items())}

    async def close(self) -> None:
        self._cache.clear()
        await self._client.close()


async def _selfcheck() -> int:
    """`python -m bravo_executor.vault [path ...]`: field names per path, 403 shown as FORBIDDEN."""
    import json
    import sys

    paths = sys.argv[1:] or ["platform/cloudflare"]
    broker = SecretBroker.from_env()
    out: dict[str, Any] = {}
    try:
        for p in paths:
            try:
                out[p] = await broker.field_names(p)
            except Forbidden:
                out[p] = "FORBIDDEN (403)"
            except VaultError as exc:
                out[p] = str(exc)
        out["_login"] = {"policies": (broker.login_facts or {}).get("policies"),
                         "lease_duration_s": (broker.login_facts or {}).get("lease_duration_s")}
    finally:
        await broker.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(_selfcheck()))
