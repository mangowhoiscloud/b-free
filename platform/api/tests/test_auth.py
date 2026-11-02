"""Cloudflare Access JWT verification and authorization rules (08 5, audit A3).

Keys are always fetched over HTTP from a certs URL, as from ``https://<team>/cdn-cgi/access/certs``: the
tests serve their own generated keys on localhost (real uvicorn servers, no mocked HTTP client). DB-backed
tests (bootstrap, audience by path on the apps, 503 when unconfigured) use the throwaway postgres.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.auth import (
    Action,
    Channel,
    CredentialSource,
    Principal,
    RemoteJWKS,
    Role,
    TokenVerifier,
    audience_for,
    authorize,
    choose_tenant,
    extract_credential,
)
from bravo_api.config import Mode, access_from_env, bootstrap_roles_from_env
from bravo_api.errors import AuthError, ForbiddenError

from .conftest import CONSOLE_AUD, ISSUER, KID, MCP_AUD, ExecutorStub, Keys, _serve, make_settings, public_jwk

BOTH = (CredentialSource.CF_ASSERTION, CredentialSource.BEARER)


@pytest.fixture()
def verifier(keys: Keys) -> TokenVerifier:
    return TokenVerifier(ISSUER, RemoteJWKS(keys.jwks_url))


async def test_valid_token(verifier: TokenVerifier, keys: Keys) -> None:
    v = await verifier.verify(keys.token("operator@bravo.test", CONSOLE_AUD), CONSOLE_AUD)
    assert v.email == "operator@bravo.test" and CONSOLE_AUD in v.aud


async def test_wrong_alg_hs256_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, alg="HS256", key="x" * 32)
    with pytest.raises(AuthError, match="alg_not_allowed"):
        await verifier.verify(token, CONSOLE_AUD)


async def test_alg_none_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    import base64
    import json

    signed = keys.token("operator@bravo.test", CONSOLE_AUD)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "test-1"}).encode()).rstrip(b"=").decode()
    token = f"{header}.{signed.split('.')[1]}."
    with pytest.raises(AuthError, match="alg_not_allowed"):
        await verifier.verify(token, CONSOLE_AUD)


async def test_other_rsa_key_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError, match="invalid_token"):
        await verifier.verify(keys.token("operator@bravo.test", CONSOLE_AUD, key=other), CONSOLE_AUD)


async def test_wrong_aud_rejected_per_entry(verifier: TokenVerifier, keys: Keys) -> None:
    mcp_token = keys.token("operator@bravo.test", MCP_AUD)
    with pytest.raises(AuthError, match="wrong_audience"):
        await verifier.verify(mcp_token, CONSOLE_AUD)
    console_token = keys.token("operator@bravo.test", CONSOLE_AUD)
    with pytest.raises(AuthError, match="wrong_audience"):
        await verifier.verify(console_token, MCP_AUD)


async def test_wrong_issuer_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, iss="https://evil.cloudflareaccess.com")
    with pytest.raises(AuthError, match="wrong_issuer"):
        await verifier.verify(token, CONSOLE_AUD)


async def test_expired_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, exp_delta=-3600)
    with pytest.raises(AuthError, match="token_expired"):
        await verifier.verify(token, CONSOLE_AUD)


@pytest.mark.parametrize("claim", ["iat", "sub", "exp"])
async def test_missing_required_claim(verifier: TokenVerifier, keys: Keys, claim: str) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, drop=(claim,))
    with pytest.raises(AuthError):
        await verifier.verify(token, CONSOLE_AUD)


async def test_service_token_without_email_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, drop=("email",))
    with pytest.raises(AuthError, match="not_a_human_principal"):
        await verifier.verify(token, CONSOLE_AUD)


async def test_unknown_kid_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    with pytest.raises(AuthError, match="unknown_kid"):
        await verifier.verify(keys.token("operator@bravo.test", CONSOLE_AUD, kid="rotated-away"), CONSOLE_AUD)


# --- JWKS from the certs URL: cache, rotation, fail closed --------------------------------------------


@dataclass
class Certs:
    """A certs endpoint whose answer the test controls. Counts real HTTP hits."""

    url: str = ""
    body: Any = None
    status: int = 200
    raw: bytes | None = None
    delay_s: float = 0.0
    hits: int = 0
    log: list[str] = field(default_factory=list)


@pytest.fixture()
def certs(keys: Keys) -> Certs:
    state = Certs(body={"keys": [public_jwk(keys.private, KID)]})
    app = FastAPI()

    @app.get("/cdn-cgi/access/certs")
    def serve() -> Response:
        state.hits += 1
        if state.delay_s:
            time.sleep(state.delay_s)
        if state.raw is not None:
            return Response(state.raw, status_code=state.status, media_type="application/json")
        return JSONResponse(state.body, status_code=state.status)

    state.url = _serve(app) + "/cdn-cgi/access/certs"
    return state


async def test_jwks_fetched_from_url_once_and_cached(certs: Certs, keys: Keys) -> None:
    v = TokenVerifier(ISSUER, RemoteJWKS(certs.url, ttl_s=300))
    for _ in range(3):
        await v.verify(keys.token("operator@bravo.test", CONSOLE_AUD), CONSOLE_AUD)
    assert certs.hits == 1
    await v.aclose()


async def test_unknown_kid_refreshes_once_then_rate_limits(certs: Certs, keys: Keys) -> None:
    jwks = RemoteJWKS(certs.url, ttl_s=300, min_refresh_s=60)
    v = TokenVerifier(ISSUER, jwks)
    await v.verify(keys.token("operator@bravo.test", CONSOLE_AUD), CONSOLE_AUD)
    # Access rotates: a second key appears at the certs URL.
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certs.body = {"keys": [public_jwk(keys.private, KID), public_jwk(rotated, "k2")]}
    await v.verify(keys.token("operator@bravo.test", CONSOLE_AUD, key=rotated, kid="k2"), CONSOLE_AUD)
    assert certs.hits == 2
    # A kid that is not there: no refetch within min_refresh_s (no hammering), refused.
    with pytest.raises(AuthError, match="unknown_kid"):
        await v.verify(keys.token("operator@bravo.test", CONSOLE_AUD, kid="nope"), CONSOLE_AUD)
    assert certs.hits == 2
    await v.aclose()


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param({"status": 500, "body": {"error": "boom"}}, id="http-500"),
        pytest.param({"raw": b"<html>not json</html>"}, id="garbage"),
        pytest.param({"body": {"no": "keys"}}, id="no-keys"),
        pytest.param({"body": {"keys": [{"kty": "EC", "kid": "e", "crv": "P-256", "x": "AA", "y": "AA"}]}}, id="no-rsa"),
        pytest.param({"delay_s": 1.5}, id="timeout"),
    ],
)
async def test_jwks_fetch_failure_fails_closed(certs: Certs, keys: Keys, setup: dict[str, Any]) -> None:
    for k, val in setup.items():
        setattr(certs, k, val)
    v = TokenVerifier(ISSUER, RemoteJWKS(certs.url, timeout_s=0.5))
    with pytest.raises(AuthError, match="jwks_unavailable"):
        await v.verify(keys.token("operator@bravo.test", CONSOLE_AUD), CONSOLE_AUD)
    await v.aclose()


async def test_expired_cache_is_not_trusted_when_refresh_fails(certs: Certs, keys: Keys) -> None:
    v = TokenVerifier(ISSUER, RemoteJWKS(certs.url, ttl_s=0.2, failure_backoff_s=0))
    token = keys.token("operator@bravo.test", CONSOLE_AUD)
    await v.verify(token, CONSOLE_AUD)
    certs.status, certs.body = 503, {"error": "down"}
    await asyncio.sleep(0.3)
    with pytest.raises(AuthError, match="jwks_unavailable"):  # stale keys are never used
        await v.verify(token, CONSOLE_AUD)
    certs.status, certs.body = 200, {"keys": [public_jwk(keys.private, KID)]}
    await v.verify(token, CONSOLE_AUD)  # back once the certs URL answers
    await v.aclose()


# --- audience by path, configuration ----------------------------------------------------------------


def test_audience_by_path() -> None:
    assert audience_for("/mcp", "c", "m") == "m"
    assert audience_for("/mcp/", "c", "m") == "m"
    for path in ("/v1/plans", "/v1/plans/x/approvals", "/", "/mcpx", "/v1/mcp"):
        assert audience_for(path, "c", "m") == "c"


def test_access_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("BRAVO_ACCESS_TEAM_DOMAIN", "BRAVO_JWT_ISSUER", "BRAVO_JWKS_URL"):
        monkeypatch.delenv(k, raising=False)
    assert access_from_env() == (None, None)  # not connected
    monkeypatch.setenv("BRAVO_ACCESS_TEAM_DOMAIN", "falling-rain-1f1c.cloudflareaccess.com")
    assert access_from_env() == (
        "https://falling-rain-1f1c.cloudflareaccess.com",
        "https://falling-rain-1f1c.cloudflareaccess.com/cdn-cgi/access/certs",
    )
    monkeypatch.setenv("BRAVO_JWT_ISSUER", "https://other.cloudflareaccess.com")
    with pytest.raises(RuntimeError, match="disagrees"):
        access_from_env()
    monkeypatch.delenv("BRAVO_ACCESS_TEAM_DOMAIN")
    monkeypatch.setenv("BRAVO_JWT_ISSUER", "http://plain.example")
    with pytest.raises(RuntimeError, match="https"):
        access_from_env()


def test_bootstrap_roles_refuse_operator_with_approver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAVO_BOOTSTRAP_ROLES", raising=False)
    assert bootstrap_roles_from_env() == ("operator", "viewer")
    for bad in ("operator,approver", "operator,security_approver", "root", ""):
        monkeypatch.setenv("BRAVO_BOOTSTRAP_ROLES", bad)
        with pytest.raises(RuntimeError):
            bootstrap_roles_from_env()
    monkeypatch.setenv("BRAVO_BOOTSTRAP_ROLES", "viewer,approver")
    assert bootstrap_roles_from_env() == ("approver", "viewer")


# --- apps: 503 when unconfigured, audience by path, no dev tokens -------------------------------------


@pytest.fixture()
def unconfigured(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Iterator[tuple[TestClient, TestClient]]:
    base = make_settings(keys, pg, executor.url, Mode.API)
    off = {"jwt_issuer": None, "jwks_url": None, "console_aud": None, "mcp_aud": None}
    with TestClient(create_app(replace(base, **off))) as api, TestClient(
        create_app(replace(make_settings(keys, pg, executor.url, Mode.MCP), **off))
    ) as mcp:
        yield api, mcp


def test_unconfigured_access_refuses_with_503_and_no_fallback(
    unconfigured: tuple[TestClient, TestClient], keys: Keys
) -> None:
    api, mcp = unconfigured
    token = keys.token("operator@bravo.test", CONSOLE_AUD)
    r = api.get(f"/v1/plans/{uuid.uuid4()}", headers={"Cf-Access-Jwt-Assertion": token})
    assert r.status_code == 503
    assert r.json() == {"error": "access_not_connected", "detail": {"message": "연결 안 됨: Cloudflare Access"}}
    r = mcp.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                 headers={"Authorization": f"Bearer {keys.token('operator@bravo.test', MCP_AUD)}",
                          "Accept": "application/json, text/event-stream"})
    assert r.status_code == 503 and r.json()["error"] == "access_not_connected"
    assert api.get("/healthz").status_code == 200  # the app still starts and reports itself


@pytest.fixture()
def apps(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Iterator[tuple[TestClient, TestClient]]:
    with TestClient(create_app(make_settings(keys, pg, executor.url, Mode.API))) as api, TestClient(
        create_app(make_settings(keys, pg, executor.url, Mode.MCP))
    ) as mcp:
        yield api, mcp


def test_one_host_audience_by_path_and_no_dev_tokens(apps: tuple[TestClient, TestClient], keys: Keys) -> None:
    api, mcp = apps
    plan = f"/v1/plans/{uuid.uuid4()}"
    ok = api.get(plan, headers={"Cf-Access-Jwt-Assertion": keys.token("operator@bravo.test", CONSOLE_AUD)})
    assert ok.status_code == 404 and ok.json()["error"] == "plan_not_found"  # authenticated, plan unknown
    r = api.get(plan, headers={"Cf-Access-Jwt-Assertion": keys.token("operator@bravo.test", MCP_AUD)})
    assert (r.status_code, r.json()["error"]) == (401, "wrong_audience")  # MCP token on /v1
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    h = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    console_tok, mcp_tok = keys.token("operator@bravo.test", CONSOLE_AUD), keys.token("operator@bravo.test", MCP_AUD)
    r = mcp.post("/mcp", json=rpc, headers={**h, "Authorization": f"Bearer {console_tok}"})
    assert (r.status_code, r.json()["error"]) == (401, "wrong_audience")  # console token on /mcp
    r = mcp.post("/mcp", json=rpc, headers={**h, "Authorization": f"Bearer {mcp_tok}"})
    assert r.status_code == 200
    token = keys.token("operator@bravo.test", CONSOLE_AUD)
    assert api.get("/v1/dev/tokens", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 404
    assert api.get("/v1/dev/tokens").status_code == 404


# --- first-login bootstrap ------------------------------------------------------------------------------


def _members(pg: dict[str, str], tenant: str) -> list[tuple[str, str]]:
    with psycopg.connect(pg["owner"]) as c:
        return [(r[0], r[1]) for r in c.execute(
            "SELECT p.email, m.role FROM memberships m JOIN principals p ON p.id = m.principal_id "
            "WHERE m.tenant_id = %s ORDER BY m.role", (tenant,))]


def test_unknown_principal_is_403_with_sub_and_email(apps: tuple[TestClient, TestClient], keys: Keys) -> None:
    api, _ = apps
    sub = f"stranger-{uuid.uuid4()}"
    r = api.get(f"/v1/plans/{uuid.uuid4()}",
                headers={"Cf-Access-Jwt-Assertion": keys.token("stranger@bravo.test", CONSOLE_AUD, sub=sub)})
    assert r.status_code == 403
    assert r.json() == {"error": "unknown_principal", "detail": {"sub": sub, "email": "stranger@bravo.test"}}


def test_bootstrap_only_that_email_and_only_into_an_empty_tenant(
    keys: Keys, pg: dict[str, str], executor: ExecutorStub
) -> None:
    tenant = "boot-" + uuid.uuid4().hex[:8]
    settings = replace(make_settings(keys, pg, executor.url, Mode.API), bootstrap_email="lead@bravo.test",
                       bootstrap_tenant=tenant, bootstrap_roles=("operator", "viewer"))
    plan = f"/v1/plans/{uuid.uuid4()}"
    with TestClient(create_app(settings)) as api:
        # Someone else first: refused, nothing created.
        r = api.get(plan, headers={"Cf-Access-Jwt-Assertion": keys.token("other@bravo.test", CONSOLE_AUD,
                                                                          sub=f"o-{uuid.uuid4()}")})
        assert (r.status_code, r.json()["error"]) == (403, "unknown_principal")
        assert _members(pg, tenant) == []
        # The named email: becomes the first member with the configured roles.
        lead_sub = f"lead-{uuid.uuid4()}"
        r = api.get(plan, headers={"Cf-Access-Jwt-Assertion": keys.token("lead@bravo.test", CONSOLE_AUD, sub=lead_sub)})
        assert (r.status_code, r.json()["error"]) == (404, "plan_not_found")
        assert _members(pg, tenant) == [("lead@bravo.test", "operator"), ("lead@bravo.test", "viewer")]
        # Same email, different identity (another sub): the tenant has members now -> admin grants only.
        r = api.get(plan, headers={"Cf-Access-Jwt-Assertion": keys.token("lead@bravo.test", CONSOLE_AUD,
                                                                          sub=f"lead2-{uuid.uuid4()}")})
        assert (r.status_code, r.json()["error"]) == (403, "unknown_principal")
        assert len(_members(pg, tenant)) == 2
    # A tenant that already has members (t1, seeded by the tests) never bootstraps.
    with TestClient(create_app(replace(settings, bootstrap_tenant="t1"))) as api:
        r = api.get(plan, headers={"Cf-Access-Jwt-Assertion": keys.token("lead@bravo.test", CONSOLE_AUD,
                                                                          sub=f"lead3-{uuid.uuid4()}")})
        assert (r.status_code, r.json()["error"]) == (403, "unknown_principal")


def test_bootstrap_function_enforces_separation_of_duties(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["bravo_api"]) as c, pytest.raises(psycopg.errors.CheckViolation,
                                                              match="bootstrap_separation_of_duties"):
        c.execute("SELECT bravo_bootstrap_principal(%s, %s, %s, %s, %s)",
                  (ISSUER, f"x-{uuid.uuid4()}", "x@bravo.test", "boot-" + uuid.uuid4().hex[:8], ["operator", "approver"]))


def test_admin_grant_refuses_operator_plus_approver(pg: dict[str, str]) -> None:
    from bravo_api.admin import grant

    tenant, sub = "adm-" + uuid.uuid4().hex[:8], f"adm-{uuid.uuid4()}"
    grant(pg["owner"], iss=ISSUER, sub=sub, email="adm@bravo.test", tenant=tenant, role="operator")
    with pytest.raises(ValueError, match="separation of duties"):
        grant(pg["owner"], iss=ISSUER, sub=sub, email="adm@bravo.test", tenant=tenant, role="approver")
    assert _members(pg, tenant) == [("adm@bravo.test", "operator")]


# --- credentials and authorize() (unchanged rules) -------------------------------------------------------


def test_two_assertion_headers_rejected() -> None:
    headers = [("Cf-Access-Jwt-Assertion", "a.b.c"), ("Cf-Access-Jwt-Assertion", "d.e.f")]
    with pytest.raises(AuthError, match="multiple_credentials"):
        extract_credential(headers, BOTH)


def test_assertion_plus_bearer_rejected() -> None:
    headers = [("Cf-Access-Jwt-Assertion", "a.b.c"), ("Authorization", "Bearer a.b.c")]
    with pytest.raises(AuthError, match="multiple_credentials"):
        extract_credential(headers, BOTH)


def test_mcp_entry_accepts_bearer_only() -> None:
    with pytest.raises(AuthError, match="credential_source_not_accepted"):
        extract_credential([("Cf-Access-Jwt-Assertion", "a.b.c")], (CredentialSource.BEARER,))
    assert extract_credential([("authorization", "Bearer a.b.c")], (CredentialSource.BEARER,)) == "a.b.c"


@pytest.mark.parametrize("value", ["Basic a.b.c", "Bearer", "Bearer notajwt"])
def test_malformed_authorization(value: str) -> None:
    with pytest.raises(AuthError):
        extract_credential([("Authorization", value)], BOTH)


def test_missing_credentials() -> None:
    with pytest.raises(AuthError, match="missing_credentials"):
        extract_credential([("Host", "x")], BOTH)


def _p(*roles: Role, channel: Channel) -> Principal:
    return Principal(id=uuid.uuid4(), iss="i", sub="s", email="e@x", tenant_id="t1", roles=frozenset(roles), channel=channel)


@pytest.mark.parametrize("action", [Action.APPROVE, Action.SHIFT])
def test_mcp_channel_can_never_approve_or_shift(action: Action) -> None:
    every_role = _p(*Role, channel=Channel.MCP)
    with pytest.raises(ForbiddenError):
        authorize(every_role, action, Channel.MCP)


def test_service_channel_denied_everything() -> None:
    p = _p(*Role, channel=Channel.SERVICE)
    for action in Action:
        with pytest.raises(ForbiddenError):
            authorize(p, action, Channel.SERVICE)


def test_channel_must_match_principal_channel() -> None:
    p = _p(Role.APPROVER, channel=Channel.MCP)
    with pytest.raises(ForbiddenError):
        authorize(p, Action.APPROVE, Channel.CONSOLE)


def test_console_roles() -> None:
    authorize(_p(Role.APPROVER, channel=Channel.CONSOLE), Action.APPROVE, Channel.CONSOLE)
    authorize(_p(Role.OPERATOR, channel=Channel.CONSOLE), Action.SHIFT, Channel.CONSOLE)
    with pytest.raises(ForbiddenError):
        authorize(_p(Role.OPERATOR, channel=Channel.CONSOLE), Action.APPROVE, Channel.CONSOLE)
    with pytest.raises(ForbiddenError):
        authorize(_p(Role.VIEWER, channel=Channel.CONSOLE), Action.GATE_RUN, Channel.CONSOLE)


def test_choose_tenant() -> None:
    assert choose_tenant([("t1", "operator")], None)[0] == "t1"
    with pytest.raises(ForbiddenError, match="tenant_ambiguous"):
        choose_tenant([("t1", "operator"), ("t2", "viewer")], None)
    assert choose_tenant([("t1", "operator"), ("t2", "viewer")], "t2") == ("t2", frozenset({Role.VIEWER}))
    with pytest.raises(ForbiddenError, match="not_a_member"):
        choose_tenant([("t1", "operator")], "t9")
