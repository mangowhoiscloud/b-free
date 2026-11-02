"""Access-style JWT verification, principal resolution and the single authorize() check.

Rules (docs/architecture/08-operator-mcp.md 5):
- RS256 only, one fixed issuer, required claims iss/aud/exp/iat/sub (+ email: human only).
- The audience is fixed per entry point: ``console-aud`` for REST, ``mcp-aud`` for MCP.
- Exactly one credential per request. Two assertion headers (or assertion + bearer) -> 401.
- The principal key is (iss, sub); roles and tenant come from ``memberships``.
- Every handler calls ``authorize(principal, action, channel)``; routers are not trusted.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
from jwt import PyJWK, PyJWKSet

from .errors import AuthError, ForbiddenError

CF_ASSERTION_HEADER = "cf-access-jwt-assertion"
AUTHORIZATION_HEADER = "authorization"
ALLOWED_ALGORITHMS = ("RS256",)
REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "sub")
LEEWAY_S = 30


class Channel(StrEnum):
    CONSOLE = "console"
    MCP = "mcp"
    SERVICE = "service"


class CredentialSource(StrEnum):
    CF_ASSERTION = "cf-access-jwt-assertion"
    BEARER = "bearer"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    SECURITY_APPROVER = "security_approver"


class Action(StrEnum):
    ENV_REGISTER = "env_register"
    ENV_READ = "env_read"
    PLAN_CREATE = "plan_create"
    PLAN_READ = "plan_read"
    REHEARSAL_START = "rehearsal_start"
    GATE_RUN = "gate_run"
    APPROVE = "approve"
    SHIFT = "shift"


_READERS = frozenset(Role)
_HUMAN = frozenset({Channel.CONSOLE, Channel.MCP})
_CONSOLE = frozenset({Channel.CONSOLE})

# action -> (channels that may call it, roles that may call it). README "공통 인가" table.
POLICY: dict[Action, tuple[frozenset[Channel], frozenset[Role]]] = {
    Action.ENV_READ: (_HUMAN, _READERS),
    Action.PLAN_READ: (_HUMAN, _READERS),
    Action.ENV_REGISTER: (_HUMAN, frozenset({Role.OPERATOR})),
    Action.PLAN_CREATE: (_HUMAN, frozenset({Role.OPERATOR})),
    Action.REHEARSAL_START: (_HUMAN, frozenset({Role.OPERATOR})),
    Action.GATE_RUN: (_HUMAN, frozenset({Role.OPERATOR})),
    Action.APPROVE: (_CONSOLE, frozenset({Role.APPROVER})),
    Action.SHIFT: (_CONSOLE, frozenset({Role.OPERATOR})),
}


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    iss: str
    sub: str
    email: str
    aud: tuple[str, ...]
    claims: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Principal:
    id: UUID
    iss: str
    sub: str
    email: str
    tenant_id: str
    roles: frozenset[Role]
    channel: Channel

    def actor(self) -> dict[str, str]:
        return {"kind": self.channel.value, "principal": self.email}


def is_allowed(principal: Principal, action: Action, channel: Channel) -> bool:
    channels, roles = POLICY[action]
    return channel in channels and principal.channel == channel and bool(principal.roles & roles)


def authorize(principal: Principal, action: Action, channel: Channel) -> None:
    """Raise ForbiddenError unless ``principal`` may perform ``action`` on ``channel``."""
    if channel is Channel.SERVICE or not is_allowed(principal, action, channel):
        raise ForbiddenError("insufficient_scope")


class JWKSStore:
    """JWKS loaded from a file. Reloads when the file changes so key rotation is followed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._keys: dict[str, PyJWK] = {}

    def _load(self) -> None:
        stat = os.stat(self._path)
        if self._mtime == stat.st_mtime and self._keys:
            return
        data = json.loads(self._path.read_text())
        keyset = PyJWKSet.from_dict(data)
        keys: dict[str, PyJWK] = {}
        for key in keyset.keys:
            if key.key_id and key.key_type == "RSA":
                keys[key.key_id] = key
        self._keys = keys
        self._mtime = stat.st_mtime

    def get(self, kid: str) -> PyJWK:
        with self._lock:
            try:
                self._load()
            except (OSError, ValueError, jwt.PyJWKSetError) as exc:
                raise AuthError("jwks_unavailable") from exc
            key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown_kid")
        return key


class TokenVerifier:
    def __init__(self, issuer: str, jwks: JWKSStore) -> None:
        self._issuer = issuer
        self._jwks = jwks

    def verify(self, token: str, audience: str) -> VerifiedToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.DecodeError as exc:
            raise AuthError("malformed_token") from exc
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise AuthError("alg_not_allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("missing_kid")
        key = self._jwks.get(kid)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=audience,
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": list(REQUIRED_CLAIMS), "verify_signature": True},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token_expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("wrong_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("wrong_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthError("missing_claim") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError("invalid_token") from exc
        sub, email = claims.get("sub"), claims.get("email")
        if not isinstance(sub, str) or not sub:
            raise AuthError("missing_claim")
        # Human identity only in P0: service tokens carry no email (08 5).
        if not isinstance(email, str) or "@" not in email:
            raise AuthError("not_a_human_principal")
        aud = claims["aud"]
        auds = tuple(aud) if isinstance(aud, list) else (str(aud),)
        return VerifiedToken(iss=claims["iss"], sub=sub, email=email, aud=auds, claims=claims)


def extract_credential(headers: Iterable[tuple[str, str]], sources: Sequence[CredentialSource]) -> str:
    """Return the single JWT presented on an accepted source, else raise AuthError.

    ``headers`` must be the raw (possibly repeated) header list.
    """
    assertions: list[str] = []
    bearers: list[str] = []
    for name, value in headers:
        lname = name.lower()
        if lname == CF_ASSERTION_HEADER:
            assertions.append(value.strip())
        elif lname == AUTHORIZATION_HEADER:
            bearers.append(value.strip())
    if len(assertions) + len(bearers) > 1:
        raise AuthError("multiple_credentials")
    if assertions:
        if CredentialSource.CF_ASSERTION not in sources:
            raise AuthError("credential_source_not_accepted")
        token = assertions[0]
    elif bearers:
        if CredentialSource.BEARER not in sources:
            raise AuthError("credential_source_not_accepted")
        scheme, _, token = bearers[0].partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthError("malformed_authorization_header")
        token = token.strip()
    else:
        raise AuthError("missing_credentials")
    if not token or token.count(".") != 2:
        raise AuthError("malformed_token")
    return token


def choose_tenant(memberships: Sequence[tuple[str, str]], requested: str | None) -> tuple[str, frozenset[Role]]:
    """Pick the tenant for this request from (tenant_id, role) rows."""
    tenants = sorted({t for t, _ in memberships})
    if not tenants:
        raise ForbiddenError("no_membership")
    if requested is not None:
        if requested not in tenants:
            raise ForbiddenError("not_a_member")
        tenant = requested
    elif len(tenants) == 1:
        tenant = tenants[0]
    else:
        raise ForbiddenError("tenant_ambiguous")
    roles = frozenset(Role(r) for t, r in memberships if t == tenant and r in Role._value2member_map_)
    if not roles:
        raise ForbiddenError("no_membership")
    return tenant, roles
