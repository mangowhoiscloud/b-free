"""Short-lived SSE tickets.

Browsers' EventSource cannot send Authorization or Cf-Access-Jwt-Assertion headers. When
the console is not behind Access (pilot dev), it first calls an authenticated endpoint for
a ticket bound to (principal, tenant, plan) for 60 s and opens the stream with ``?ticket=``.
The ticket is HMAC-SHA256 signed; it carries no secret and grants read of one plan only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from .errors import AuthError

TTL_S = 60


@dataclass(frozen=True, slots=True)
class Ticket:
    principal_id: UUID
    tenant_id: str
    plan_id: UUID


class TicketSigner:
    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret or secrets.token_bytes(32)

    def issue(self, principal_id: UUID, tenant_id: str, plan_id: UUID, now: float | None = None) -> str:
        body = {"p": str(principal_id), "t": tenant_id, "plan": str(plan_id), "exp": int((now or time.time()) + TTL_S)}
        raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(hmac.new(self._secret, raw, hashlib.sha256).digest()).rstrip(b"=")
        return f"{raw.decode()}.{sig.decode()}"

    def verify(self, ticket: str, plan_id: UUID, now: float | None = None) -> Ticket:
        try:
            raw, sig = ticket.split(".", 1)
            expected = base64.urlsafe_b64encode(hmac.new(self._secret, raw.encode(), hashlib.sha256).digest()).rstrip(b"=")
            if not hmac.compare_digest(expected.decode(), sig):
                raise AuthError("invalid_ticket")
            body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthError("invalid_ticket") from exc
        if int(body["exp"]) < int(now or time.time()):
            raise AuthError("ticket_expired")
        if body["plan"] != str(plan_id):
            raise AuthError("ticket_wrong_plan")
        return Ticket(principal_id=UUID(body["p"]), tenant_id=body["t"], plan_id=plan_id)
