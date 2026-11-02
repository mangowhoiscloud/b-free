"""HTTP clients for the optional upstreams: platform/gate controls, platform/collector
inventory and the platform/rehearsal service. No caller token is ever forwarded.

Each client raises ``UpstreamError(reason)`` with a stable reason code. The state machine
turns a reason into ``state_reason`` or a BLOCK input; it never substitutes a value.
"""

from __future__ import annotations

from typing import Any

import httpx

VALID_GRADES = frozenset({"config_swap", "adapter", "code_change", "no_change", "unclassified"})
GAP_GRADES = frozenset({"code_change", "unclassified"})  # need a proposal (FR-2, §8.1)


class UpstreamError(Exception):
    def __init__(self, reason: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.body = body


class _Client:
    name = "upstream"

    def __init__(
        self, base_url: str, timeout_s: float, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        headers = {"User-Agent": "bravo-api"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)),
            transport=transport,
            headers=headers,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise UpstreamError(f"{self.name}_unreachable") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{self.name}_response_lost") from exc

    @staticmethod
    def _json(resp: httpx.Response, name: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(f"{name}_bad_response", resp.status_code) from exc


class GateControlsClient(_Client):
    """``GET /v1/controls`` on platform/gate: same schema as the Executor's controls."""

    name = "gate_controls"

    async def controls(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/v1/controls")
        if resp.status_code != 200:
            raise UpstreamError(f"gate_controls_http_{resp.status_code}", resp.status_code)
        body = self._json(resp, self.name)
        if not isinstance(body, list):
            raise UpstreamError("gate_controls_bad_response")
        return body


class CollectorClient(_Client):
    """``GET /v1/inventory`` on platform/collector (FR-1, FR-2).

    Expected body (to be fixed in the 11/3 collector contract): ``{"collected_at": str,
    "dependencies": [{"id": str, "kind": str, "grade": <VALID_GRADES>, ...}]}``. Anything
    else is ``collector_bad_response``: ANALYZE does not guess.
    """

    name = "collector"

    async def inventory(self) -> dict[str, Any]:
        resp = await self._request("GET", "/v1/inventory")
        if resp.status_code != 200:
            raise UpstreamError(f"collector_http_{resp.status_code}", resp.status_code)
        body = self._json(resp, self.name)
        deps = body.get("dependencies") if isinstance(body, dict) else None
        if not isinstance(deps, list):
            raise UpstreamError("collector_bad_response")
        for d in deps:
            if not isinstance(d, dict) or not isinstance(d.get("id"), str) or d.get("grade") not in VALID_GRADES:
                raise UpstreamError("collector_bad_response")
        return body


class RehearsalClient(_Client):
    """platform/rehearsal service: ``POST /v1/trials`` (Idempotency-Key) and ``GET /v1/trials/{id}``."""

    name = "rehearsal"

    async def start(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        resp = await self._request("POST", "/v1/trials", json=payload, headers={"Idempotency-Key": idempotency_key})
        body = self._json(resp, self.name) if resp.content else {}
        if resp.status_code in (200, 201, 202) and isinstance(body, dict) and isinstance(body.get("trial_id"), str):
            return body
        reason = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else None
        raise UpstreamError(reason or f"rehearsal_http_{resp.status_code}", resp.status_code, body)

    async def get(self, trial_id: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/v1/trials/{trial_id}")
        if resp.status_code == 404:
            raise UpstreamError("trial_lost", 404)
        if resp.status_code != 200:
            raise UpstreamError(f"rehearsal_http_{resp.status_code}", resp.status_code)
        body = self._json(resp, self.name)
        if not isinstance(body, dict):
            raise UpstreamError("rehearsal_bad_response")
        return body
