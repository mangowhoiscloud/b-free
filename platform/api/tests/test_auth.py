"""JWT verification and authorization rules (08 5). No database needed."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from bravo_api.auth import (
    Action,
    Channel,
    CredentialSource,
    JWKSStore,
    Principal,
    Role,
    TokenVerifier,
    authorize,
    choose_tenant,
    extract_credential,
)
from bravo_api.errors import AuthError, ForbiddenError

from .conftest import CONSOLE_AUD, MCP_AUD, Keys

BOTH = (CredentialSource.CF_ASSERTION, CredentialSource.BEARER)


@pytest.fixture()
def verifier(keys: Keys) -> TokenVerifier:
    from .conftest import ISSUER

    return TokenVerifier(ISSUER, JWKSStore(keys.jwks_path))


def test_valid_token(verifier: TokenVerifier, keys: Keys) -> None:
    v = verifier.verify(keys.token("operator@bravo.test", CONSOLE_AUD), CONSOLE_AUD)
    assert v.email == "operator@bravo.test" and CONSOLE_AUD in v.aud


def test_wrong_alg_hs256_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, alg="HS256", key="x" * 32)
    with pytest.raises(AuthError, match="alg_not_allowed"):
        verifier.verify(token, CONSOLE_AUD)


def test_alg_none_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    import base64
    import json

    signed = keys.token("operator@bravo.test", CONSOLE_AUD)
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "test-1"}).encode()).rstrip(b"=").decode()
    token = f"{header}.{signed.split('.')[1]}."
    with pytest.raises(AuthError, match="alg_not_allowed"):
        verifier.verify(token, CONSOLE_AUD)


def test_other_rsa_key_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError, match="invalid_token"):
        verifier.verify(keys.token("operator@bravo.test", CONSOLE_AUD, key=other), CONSOLE_AUD)


def test_wrong_aud_rejected_per_entry(verifier: TokenVerifier, keys: Keys) -> None:
    mcp_token = keys.token("operator@bravo.test", MCP_AUD)
    with pytest.raises(AuthError, match="wrong_audience"):
        verifier.verify(mcp_token, CONSOLE_AUD)
    console_token = keys.token("operator@bravo.test", CONSOLE_AUD)
    with pytest.raises(AuthError, match="wrong_audience"):
        verifier.verify(console_token, MCP_AUD)


def test_wrong_issuer_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, iss="https://evil.cloudflareaccess.com")
    with pytest.raises(AuthError, match="wrong_issuer"):
        verifier.verify(token, CONSOLE_AUD)


def test_expired_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, exp_delta=-3600)
    with pytest.raises(AuthError, match="token_expired"):
        verifier.verify(token, CONSOLE_AUD)


@pytest.mark.parametrize("claim", ["iat", "sub", "exp"])
def test_missing_required_claim(verifier: TokenVerifier, keys: Keys, claim: str) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, drop=(claim,))
    with pytest.raises(AuthError):
        verifier.verify(token, CONSOLE_AUD)


def test_service_token_without_email_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    token = keys.token("operator@bravo.test", CONSOLE_AUD, drop=("email",))
    with pytest.raises(AuthError, match="not_a_human_principal"):
        verifier.verify(token, CONSOLE_AUD)


def test_unknown_kid_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    with pytest.raises(AuthError, match="unknown_kid"):
        verifier.verify(keys.token("operator@bravo.test", CONSOLE_AUD, kid="rotated-away"), CONSOLE_AUD)


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
    import uuid

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
