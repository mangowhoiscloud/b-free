"""HTTP client for the Executor (CONTRACT.md 6). The platform never forwards caller tokens.

Failure classes matter for safety (§8.6, NFR-3):
- ``ExecutorUnreachable``: the request provably did not reach the Executor (connect error).
- ``ExecutorAmbiguous``: it may have been applied (timeout after send, 5xx, bad body).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx


class ExecutorError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExecutorUnreachable(ExecutorError):
    pass


class ExecutorAmbiguous(ExecutorError):
    pass


class ExecutorClient:
    def __init__(self, base_url: str, timeout_s: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)),
            transport=transport,
            headers={"User-Agent": "bravo-api"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str) -> Any:
        try:
            resp = await self._client.get(path)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ExecutorUnreachable("executor_unreachable") from exc
        except httpx.HTTPError as exc:
            raise ExecutorUnreachable("executor_unreachable") from exc
        if resp.status_code != 200:
            raise ExecutorUnreachable(f"executor_http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ExecutorUnreachable("executor_bad_response") from exc

    async def controls(self) -> list[dict[str, Any]]:
        body = await self._get_json("/v1/controls")
        if not isinstance(body, list):
            raise ExecutorUnreachable("executor_bad_response")
        return body

    async def readiness(self) -> dict[str, Any]:
        body = await self._get_json("/v1/readiness")
        if not isinstance(body, dict):
            raise ExecutorUnreachable("executor_bad_response")
        return body

    async def run(self, run_id: UUID) -> dict[str, Any]:
        body = await self._get_json(f"/v1/runs/{run_id}")
        if not isinstance(body, dict):
            raise ExecutorUnreachable("executor_bad_response")
        return body

    async def start_cutover(
        self, *, plan_id: UUID, epoch: int, approval_id: UUID, write_pause_budget_ms: int, idempotency_key: str
    ) -> UUID:
        return await self._start("/v1/cutover", plan_id, epoch, approval_id, write_pause_budget_ms, idempotency_key)

    async def start_failback(
        self, *, plan_id: UUID, epoch: int, approval_id: UUID, write_pause_budget_ms: int, idempotency_key: str
    ) -> UUID:
        """``POST /v1/failback``: same contract as ``/v1/cutover`` (§8.6 failback row, S4)."""
        return await self._start("/v1/failback", plan_id, epoch, approval_id, write_pause_budget_ms, idempotency_key)

    async def start_shift(self, *, plan_id: UUID, epoch: int, approval_id: UUID, idempotency_key: str) -> UUID:
        """``POST /v1/front/shift`` for a plan's SHIFT (contract 15 §10): Edge weights 10/50/100 % to the
        destination, judged on real chat writes. The Executor checks the cutover approval like a cutover's."""
        payload = {"target_env": "dst", "steps": [10, 50, 100], "plan_id": str(plan_id), "epoch": epoch,
                   "approval_id": str(approval_id), "stage": "cutover"}
        return await self._post("/v1/front/shift", payload, idempotency_key)

    async def _start(
        self, path: str, plan_id: UUID, epoch: int, approval_id: UUID, write_pause_budget_ms: int, idempotency_key: str
    ) -> UUID:
        payload = {
            "plan_id": str(plan_id),
            "epoch": epoch,
            "approval_id": str(approval_id),
            "write_pause_budget_ms": write_pause_budget_ms,
        }
        return await self._post(path, payload, idempotency_key)

    async def _post(self, path: str, payload: dict[str, Any], idempotency_key: str) -> UUID:
        try:
            resp = await self._client.post(path, json=payload, headers={"Idempotency-Key": idempotency_key})
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ExecutorUnreachable("executor_unreachable") from exc
        except httpx.HTTPError as exc:
            raise ExecutorAmbiguous("executor_response_lost") from exc
        if 400 <= resp.status_code < 500:
            code = None
            try:
                detail = resp.json().get("detail")
                code = detail.get("code") if isinstance(detail, dict) else None
            except (ValueError, AttributeError):
                code = None
            # The Executor's refusal code (e.g. APPROVAL_NOT_STARTED, SHIFT_NOT_DONE) is the reason shown.
            raise ExecutorUnreachable(f"executor_rejected_{resp.status_code}" + (f":{code}" if code else ""))
        if resp.status_code not in (200, 201, 202):
            raise ExecutorAmbiguous(f"executor_http_{resp.status_code}")
        try:
            return UUID(str(resp.json()["run_id"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ExecutorAmbiguous("executor_bad_response") from exc
