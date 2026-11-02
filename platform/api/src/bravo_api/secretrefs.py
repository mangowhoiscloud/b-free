"""Resolve an environment's ``secret_ref`` against the platform OpenBao (audit B11, D-26).

The API never reads a secret value. It asks OpenBao for the KV v2 **metadata** of the reference
(``GET /v1/<mount>/metadata/<ref>``): it exists and has a live version -> ``resolved``. No such path ->
``not_found`` (registration is refused). Anything else -> ``unverified`` with the reason, shown as
"연결 안 됨: OpenBao" and a plan cannot leave COLLECT with it.

Credentials come from files mounted for this process, never from the request or the code:
``BRAVO_BAO_ROLE_ID_FILE`` + ``BRAVO_BAO_SECRET_ID_FILE`` (AppRole login, short token per check) or
``BRAVO_BAO_TOKEN_FILE`` (a token sink, e.g. an OpenBao agent). Needed policy: read on
``<mount>/metadata/tenants/*`` only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger("bravo_api.secretrefs")
NOT_CONNECTED = "openbao_not_connected"


def _read(path: str) -> str:
    return Path(path).read_text().strip()


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str  # resolved | not_found | unverified
    reason: str | None

    def as_json(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class BaoSettings:
    addr: str | None
    cacert: str | None
    kv_mount: str
    role_id_file: str | None
    secret_id_file: str | None
    token_file: str | None

    @property
    def configured(self) -> bool:
        return bool(self.addr) and (bool(self.token_file) or (bool(self.role_id_file) and bool(self.secret_id_file)))


class LoginRefused(Exception):
    pass


class SecretRefResolver:
    def __init__(self, cfg: BaoSettings, timeout_s: float = 3.0, transport: httpx.AsyncBaseTransport | None = None):
        self._cfg = cfg
        self._client = (
            httpx.AsyncClient(
                base_url=cfg.addr.rstrip("/"),
                timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)),
                verify=cfg.cacert or True,
                transport=transport,
            )
            if cfg.configured and cfg.addr
            else None
        )

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _token(self) -> tuple[str, bool]:
        """(token, revoke_after). AppRole tokens are revoked after the check."""
        assert self._client is not None
        if self._cfg.token_file:
            return (await asyncio.to_thread(_read, self._cfg.token_file)), False
        role_id = await asyncio.to_thread(_read, self._cfg.role_id_file or "")
        secret_id = await asyncio.to_thread(_read, self._cfg.secret_id_file or "")
        r = await self._client.post("/v1/auth/approle/login", json={"role_id": role_id, "secret_id": secret_id})
        if r.status_code != 200:
            raise LoginRefused(f"approle_login_http_{r.status_code}")
        return r.json()["auth"]["client_token"], True

    async def resolve(self, ref: str) -> Resolution:
        if self._client is None:
            return Resolution("unverified", NOT_CONNECTED)
        try:
            token, revoke = await self._token()
        except LoginRefused as exc:
            return Resolution("unverified", str(exc))
        except (OSError, KeyError, ValueError) as exc:
            return Resolution("unverified", f"openbao_credential_unavailable:{type(exc).__name__}")
        except httpx.HTTPError:
            return Resolution("unverified", "openbao_unreachable")
        headers = {"X-Vault-Token": token}
        try:
            r = await self._client.get(f"/v1/{self._cfg.kv_mount}/metadata/{ref}", headers=headers)
        except httpx.HTTPError:
            return Resolution("unverified", "openbao_unreachable")
        finally:
            if revoke:
                try:
                    await self._client.post("/v1/auth/token/revoke-self", headers=headers)
                except httpx.HTTPError:
                    LOG.warning("could not revoke the secret_ref check token")
        if r.status_code == 404:
            return Resolution("not_found", "secret_ref_not_found")
        if r.status_code in (401, 403):
            return Resolution("unverified", "openbao_denied")
        if r.status_code != 200:
            return Resolution("unverified", f"openbao_http_{r.status_code}")
        data = (r.json() or {}).get("data") or {}
        current = str(data.get("current_version") or "")
        version = (data.get("versions") or {}).get(current) or {}
        if not current or version.get("destroyed") or version.get("deletion_time"):
            return Resolution("not_found", "secret_ref_deleted")
        return Resolution("resolved", None)
