"""Cloudflare Access JWT verification, principal resolution and the single authorize() check.

Rules (docs/architecture/08-operator-mcp.md 5, audit A3):
- RS256 only, one fixed issuer (the Access team domain), required claims iss/aud/exp/iat/sub
  (+ email: human only).
- Keys come only from the Access certs URL (``RemoteJWKS``): cached for a TTL, refreshed on an
  unknown ``kid`` (rate-limited), and any fetch or parse failure refuses the request
  (``jwks_unavailable``). There is no file or dev key source in the product path.
- Console and MCP share one host, so the allowed audience is chosen by path (``audience_for``):
  ``/mcp`` -> MCP AUD, everything else -> console AUD.
- Unconfigured Access -> every authenticated request is refused with 503 ``access_not_connected``
  ("연결 안 됨: Cloudflare Access"). No fallback.
- Exactly one credential per request. Two assertion headers (or assertion + bearer) -> 401.
- The principal key is (iss, sub); roles and tenant come from ``memberships``.
- Every handler calls ``authorize(principal, action, channel)``; routers are not trusted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx
import jwt
from jwt import PyJWK

from .errors import AuthError, ForbiddenError

CF_ASSERTION_HEADER = "cf-access-jwt-assertion"
AUTHORIZATION_HEADER = "authorization"
ALLOWED_ALGORITHMS = ("RS256",)
REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "sub")
LEEWAY_S = 30
MCP_PATH = "/mcp"
ACCESS_NOT_CONNECTED = "access_not_connected"
ACCESS_NOT_CONNECTED_MESSAGE = "연결 안 됨: Cloudflare Access"
LOG = logging.getLogger("bravo_api.auth")


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
    CHANGE_REQUEST = "change_request"  # ask the T31 pipeline to prepare a candidate again (console only)


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
    Action.CHANGE_REQUEST: (_CONSOLE, frozenset({Role.OPERATOR})),
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


class AccessNotConnectedError(Exception):
    """Access is not configured for this deployment. HTTP 503, never a fallback login."""

    reason = ACCESS_NOT_CONNECTED
    message = ACCESS_NOT_CONNECTED_MESSAGE


def audience_for(path: str, console_aud: str, mcp_aud: str) -> str:
    """One host serves console, ``/v1`` and ``/mcp``: ``/mcp`` takes the MCP AUD, the rest the console AUD."""
    return mcp_aud if path == MCP_PATH or path.startswith(MCP_PATH + "/") else console_aud


class JWKSSource(Protocol):
    async def get(self, kid: str) -> PyJWK: ...


def parse_jwks(body: Any) -> dict[str, PyJWK]:
    """RS256 signing keys with a ``kid`` from a JWKS document. Anything unusable -> AuthError."""
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        raise AuthError("jwks_unavailable")
    out: dict[str, PyJWK] = {}
    for item in keys:
        if not isinstance(item, dict) or item.get("kty") != "RSA" or not isinstance(item.get("kid"), str):
            continue
        if item.get("alg", "RS256") != "RS256" or item.get("use", "sig") != "sig":
            continue
        try:
            out[item["kid"]] = PyJWK.from_dict(item, algorithm="RS256")
        except (jwt.PyJWKError, ValueError, TypeError):
            continue
    if not out:
        raise AuthError("jwks_unavailable")
    return out


class RemoteJWKS:
    """Access signing keys from the certs URL. Fail closed: no keys -> no login.

    - Cached for ``ttl_s``. After that the next request refetches; if the fetch fails the request is
      refused (``jwks_unavailable``) instead of trusting stale keys.
    - An unknown ``kid`` triggers one refetch, at most every ``min_refresh_s`` (key rotation).
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_s: float = 300.0,
        min_refresh_s: float = 30.0,
        timeout_s: float = 5.0,
        failure_backoff_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self._ttl = ttl_s
        self._min_refresh = min_refresh_s
        self._backoff = failure_backoff_s
        self._failed_at: float | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0)),
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "bravo-api", "Accept": "application/json"},
        )
        self._lock = asyncio.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._kid_refresh_at: float | None = None  # last refetch caused by an unknown kid
        self.fetches = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < self._ttl

    async def _fetch(self) -> None:
        now = time.monotonic()
        if self._failed_at is not None and now - self._failed_at < self._backoff:
            raise AuthError("jwks_unavailable")  # recent failure: refuse without hammering the certs URL
        self.fetches += 1
        try:
            resp = await self._client.get(self.url)
            if resp.status_code != 200:
                raise AuthError("jwks_unavailable")
            keys = parse_jwks(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            self._keys, self._fetched_at, self._failed_at = {}, None, time.monotonic()
            LOG.warning("jwks fetch failed: %s", type(exc).__name__)
            raise AuthError("jwks_unavailable") from exc
        except AuthError:
            self._keys, self._fetched_at, self._failed_at = {}, None, time.monotonic()
            LOG.warning("jwks fetch failed: unusable response")
            raise
        self._keys, self._fetched_at, self._failed_at = keys, time.monotonic(), None

    async def get(self, kid: str) -> PyJWK:
        if not self._fresh(time.monotonic()):
            async with self._lock:
                if not self._fresh(time.monotonic()):
                    await self._fetch()
        key = self._keys.get(kid)
        if key is None:
            async with self._lock:
                key = self._keys.get(kid)
                now = time.monotonic()
                if key is None and (self._kid_refresh_at is None or now - self._kid_refresh_at >= self._min_refresh):
                    self._kid_refresh_at = now
                    await self._fetch()
                    key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown_kid")
        return key


class TokenVerifier:
    def __init__(self, issuer: str, jwks: JWKSSource) -> None:
        self._issuer = issuer
        self._jwks = jwks

    async def aclose(self) -> None:
        close = getattr(self._jwks, "aclose", None)
        if close is not None:
            await close()

    async def verify(self, token: str, audience: str) -> VerifiedToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.DecodeError as exc:
            raise AuthError("malformed_token") from exc
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise AuthError("alg_not_allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("missing_kid")
        key = await self._jwks.get(kid)
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


class NotConnectedVerifier:
    """Used when Access is not configured: every authenticated request -> 503 ``access_not_connected``."""

    async def aclose(self) -> None:
        return None

    async def verify(self, token: str, audience: str) -> VerifiedToken:
        raise AccessNotConnectedError()


Verifier = TokenVerifier | NotConnectedVerifier


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
