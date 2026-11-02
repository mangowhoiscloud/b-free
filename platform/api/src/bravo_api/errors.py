"""Error types shared by REST and MCP handlers.

Layering (08 4): authentication failures -> 401, missing role -> 403, business rejections
-> a reason code (REST 409/422, MCP ``isError: true``), malformed input -> 400/JSON-RPC error.
"""

from __future__ import annotations

from typing import Any


class AuthError(Exception):
    """Credential missing, malformed or invalid. Maps to HTTP 401."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ForbiddenError(Exception):
    """Authenticated, but the role or channel may not perform the action. HTTP 403."""

    def __init__(self, reason: str = "insufficient_scope", detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class NotFoundError(Exception):
    def __init__(self, what: str) -> None:
        super().__init__(f"{what}_not_found")
        self.reason = f"{what}_not_found"


class BusinessError(Exception):
    """A deterministic rejection the caller can act on (state mismatch, stale approval...)."""

    def __init__(self, reason: str, status: int = 409, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.detail = detail or {}


class IdempotencyConflict(BusinessError):
    def __init__(self) -> None:
        super().__init__("idempotency_key_reuse", status=409)
