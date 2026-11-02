"""HTTP clients for the optional upstreams: platform/gate controls, platform/collector
inventory and the platform/rehearsal service. No caller token is ever forwarded.

Each client raises ``UpstreamError(reason)`` with a stable reason code. The state machine
turns a reason into ``state_reason`` or a BLOCK input; it never substitutes a value.
"""

from __future__ import annotations

import re
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


INVENTORY_SCHEMA = "inventory/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_TIMEOUT_S = 60.0
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024  # same cap as platform/rehearsal snapshot ingest
# Finding fields the platform reads (collector README "의존성 인벤토리", detector.Finding).
FINDING_STR_FIELDS = ("id", "rule_id", "service", "kind", "file")


def _finding_ok(f: Any) -> bool:
    return (
        isinstance(f, dict)
        and all(isinstance(f.get(k), str) and f[k] for k in FINDING_STR_FIELDS)
        and f.get("tier_code") in VALID_GRADES
        and isinstance(f.get("line"), int)
        and not isinstance(f.get("line"), bool)
    )


COST_SCHEMA = "cost/v1"
COST_TIMEOUT_S = 150.0
ROOT_EMPTY = "inventory_root_empty"  # collector: nothing mounted to scan (503), on /v1/inventory and /snapshot


def _collector_code(resp: httpx.Response) -> str | None:
    """The collector's reason code from a 503 body: ``{"error": code}`` or FastAPI ``{"detail": "code: text"}``."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    raw = body.get("error") if isinstance(body.get("error"), str) else body.get("detail")
    return raw.split(":", 1)[0].strip() if isinstance(raw, str) else None


class CollectorClient(_Client):
    """platform/collector: ``GET /v1/inventory`` (FR-2, T16) and the repo snapshot that matches it.

    The body is ``inventory/v1`` as the collector writes it (``detector.InventoryReport``): ``schema_version``,
    ``input_digest`` (64 hex), ``summary`` (object), ``findings[]`` and ``unclassified[]`` whose items carry
    ``id``, ``rule_id``, ``tier_code`` (one of VALID_GRADES), ``service``, ``kind``, ``file``, ``line``.
    Anything else is ``collector_bad_response``: ANALYZE and the rehearsal never guess. Returned unchanged.
    """

    name = "collector"

    async def clusters(self) -> dict[str, Any]:
        """``GET /v1/clusters``: live per-environment cluster view (get/list only). ``cluster_id`` is the
        collector registry's environment id, which is the environment name here (CLOUD-E2E-CONTRACT 2)."""
        resp = await self._request("GET", "/v1/clusters")
        if resp.status_code != 200:
            raise UpstreamError(f"collector_http_{resp.status_code}", resp.status_code)
        body = self._json(resp, self.name)
        if not isinstance(body, dict) or not isinstance(body.get("clusters"), list):
            raise UpstreamError("collector_bad_response")
        return body

    async def cost(self, params: dict[str, str]) -> dict[str, Any]:
        """``GET /v1/cost``: the cost receipt ``cost/v1`` (T1, NFR-8). The first fill after a collector restart reads
        every provider and the price lists (up to about a minute), so this call has its own longer timeout."""
        resp = await self._request("GET", "/v1/cost", params=params,
                                   timeout=httpx.Timeout(COST_TIMEOUT_S, connect=2.0))
        if resp.status_code == 422:
            raise UpstreamError("cost_bad_request", 422, self._json(resp, self.name))
        if resp.status_code != 200:
            raise UpstreamError(f"collector_http_{resp.status_code}", resp.status_code)
        body = self._json(resp, self.name)
        if not isinstance(body, dict) or body.get("schema_version") != COST_SCHEMA:
            raise UpstreamError("collector_bad_response")
        return body

    async def inventory(self) -> dict[str, Any]:
        resp = await self._request("GET", "/v1/inventory")
        if resp.status_code == 503 and _collector_code(resp) == ROOT_EMPTY:
            # Nothing mounted to scan: the repository is missing, not "no dependencies" and not unreachable.
            raise UpstreamError(ROOT_EMPTY, 503)
        if resp.status_code != 200:
            raise UpstreamError(f"collector_http_{resp.status_code}", resp.status_code)
        body = self._json(resp, self.name)
        if (
            not isinstance(body, dict)
            or body.get("schema_version") != INVENTORY_SCHEMA
            or not isinstance(body.get("input_digest"), str)
            or not HEX64.match(body["input_digest"])
            or not isinstance(body.get("summary"), dict)
            or not isinstance(body.get("findings"), list)
            or not isinstance(body.get("unclassified"), list)
            or not all(_finding_ok(f) for f in [*body["findings"], *body["unclassified"]])
        ):
            raise UpstreamError("collector_bad_response")
        return body

    async def snapshot(self, input_digest: str) -> bytes:
        """The customer repo files the inventory scanned, as a tar.gz (PROPOSED collector endpoint).

        ``GET /v1/inventory/snapshot`` -> 200, body = tar.gz of exactly the scanned files (the selection of
        ``bravo_rehearsal.snapshot.pack``), header ``X-Bravo-Inventory-Digest`` = that inventory's
        ``input_digest``. The collector does not serve it yet: 404/405 -> ``snapshot_unavailable``. A body
        for another inventory -> ``snapshot_inventory_mismatch``. The rehearsal service re-verifies the files.
        """
        try:
            resp = await self._client.request("GET", "/v1/inventory/snapshot", timeout=SNAPSHOT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise UpstreamError("snapshot_unavailable") from exc
        if resp.status_code == 503 and _collector_code(resp) == ROOT_EMPTY:
            raise UpstreamError(ROOT_EMPTY, 503)
        if resp.status_code != 200:
            raise UpstreamError("snapshot_unavailable", resp.status_code)
        if resp.headers.get("x-bravo-inventory-digest") != input_digest:
            raise UpstreamError("snapshot_inventory_mismatch", resp.status_code)
        if not resp.content or len(resp.content) > MAX_SNAPSHOT_BYTES:
            raise UpstreamError("snapshot_unavailable", resp.status_code)
        return resp.content


# Reasons platform/rehearsal gives for refusing a snapshot (``SnapshotRejected``) or its request.
SNAPSHOT_REFUSALS = frozenset(
    {
        "snapshot_digest_mismatch",
        "snapshot_unreadable",
        "snapshot_too_large",
        "snapshot_too_many_files",
        "snapshot_file_too_large",
        "snapshot_special_member",
        "bad_inventory_digest",
        "bad_snapshot_key",
        "inventory_digest_missing",
        "unauthorized",
    }
)


class RehearsalClient(_Client):
    """platform/rehearsal service: ``PUT /v1/snapshots/{tenant}/{plan}``, ``POST /v1/trials`` (Idempotency-Key)
    and ``GET /v1/trials/{id}``."""

    name = "rehearsal"

    async def put_snapshot(self, tenant_id: str, plan_id: str, archive: bytes, input_digest: str) -> dict[str, Any]:
        """Upload the customer repo snapshot bound to the collector inventory digest. Returns the manifest
        ``{tenant_id, plan_id, input_digest, files, withheld}``. Refusals keep the service's reason code."""
        try:
            resp = await self._client.request(
                "PUT",
                f"/v1/snapshots/{tenant_id}/{plan_id}",
                content=archive,
                headers={"X-Bravo-Inventory-Digest": input_digest, "Content-Type": "application/gzip"},
                timeout=SNAPSHOT_TIMEOUT_S,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise UpstreamError("rehearsal_unreachable") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("snapshot_upload_failed") from exc
        body = self._json(resp, self.name) if resp.content else {}
        if resp.status_code == 200 and isinstance(body, dict) and body.get("input_digest") == input_digest:
            return body
        reason = body.get("error") if isinstance(body, dict) else None
        raise UpstreamError(reason if reason in SNAPSHOT_REFUSALS else "snapshot_upload_failed", resp.status_code, body)

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
