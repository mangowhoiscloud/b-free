"""Trial token (07 3 "Trial 토큰"): one signed token per Trial, checked on every MCP call.

Format: ``base64url(json claims) "." base64url(HMAC-SHA256(key, payload))``. Claims:
``trial_id``, ``tenant_id``, ``plan_id``, ``attempt``, ``namespace``, ``ops`` (approved
scope), ``budget_usd``, ``exp`` (= Trial deadline, epoch seconds). The key comes from the
rehearsal service's environment only and never reaches the agent Pod. Verification checks
the signature and expiry here; the caller additionally checks that the Trial is running and
that the token's ``trial_id`` matches the Trial it addresses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

REQUIRED = ("trial_id", "tenant_id", "plan_id", "attempt", "namespace", "ops", "budget_usd", "exp")


class TokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TrialClaims:
    trial_id: str
    tenant_id: str
    plan_id: str
    attempt: int
    namespace: str
    ops: tuple[str, ...]
    budget_usd: float
    exp: int


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(key: bytes, claims: TrialClaims) -> str:
    if len(key) < 32:
        raise ValueError("trial token key must be at least 32 bytes")
    body = {k: getattr(claims, k) for k in REQUIRED}
    body["ops"] = list(claims.ops)
    payload = _b64(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify(key: bytes, token: str, now: float | None = None) -> TrialClaims:
    try:
        payload, sig = token.split(".")
    except ValueError as exc:
        raise TokenError("token_malformed") from exc
    expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise TokenError("token_bad_signature")
    try:
        body: dict[str, Any] = json.loads(_unb64(payload))
    except ValueError as exc:
        raise TokenError("token_malformed") from exc
    if not isinstance(body, dict) or any(k not in body for k in REQUIRED):
        raise TokenError("token_malformed")
    if not isinstance(body["exp"], int) or body["exp"] <= (now if now is not None else time.time()):
        raise TokenError("token_expired")
    return TrialClaims(
        trial_id=str(body["trial_id"]),
        tenant_id=str(body["tenant_id"]),
        plan_id=str(body["plan_id"]),
        attempt=int(body["attempt"]),
        namespace=str(body["namespace"]),
        ops=tuple(str(o) for o in body["ops"]),
        budget_usd=float(body["budget_usd"]),
        exp=int(body["exp"]),
    )
