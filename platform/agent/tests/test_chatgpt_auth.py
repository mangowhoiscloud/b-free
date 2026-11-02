"""openai_agents ChatGPT 계정 모드: 세션 레코드, 만료 판단, 갱신 요청(Codex 소스와 같은 모양), 백엔드 헤더."""
import base64
import json

import pytest

from bravo_agents import chatgpt_auth as ca
from bravo_agents.errors import AgentRunError

NOW = 1_800_000_000


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def id_token(account="acct-1234", plan="pro", fedramp=False) -> str:
    return jwt({"email": "lead@example.com", "https://api.openai.com/auth": {
        "chatgpt_account_id": account, "chatgpt_plan_type": plan, "chatgpt_account_is_fedramp": fedramp}})


def record(exp=NOW + 3600, **kw) -> dict:
    base = {"access_token": jwt({"exp": exp}), "refresh_token": "rt-old", "id_token": id_token(),
            "account_id": "acct-1234", "auth_mode": "chatgpt", "expires_at": ca.iso(exp)}
    base.update(kw)
    return base


def test_session_from_record_reads_account_expiry_plan_and_hides_token():
    s = ca.session_from_record(record())
    assert (s.account_id, s.expires_at, s.plan, s.fedramp) == ("acct-1234", NOW + 3600, "pro", False)
    assert s.access_token not in repr(s) and "rt-old" not in repr(s)


@pytest.mark.parametrize("drop", ["access_token", "refresh_token", "id_token", "account_id", "auth_mode"])
def test_incomplete_session_is_unconfigured(drop):
    rec = record()
    rec.pop(drop)
    with pytest.raises(AgentRunError) as e:
        ca.session_from_record(rec)
    assert e.value.code == "llm_unconfigured"


def test_needs_refresh_uses_jwt_exp_and_at_least_codex_margin():
    assert not ca.needs_refresh(record(exp=NOW + 3600), NOW, 600)
    assert ca.needs_refresh(record(exp=NOW + 3600), NOW, 3600)          # 실행 시간보다 짧게 남음
    assert ca.needs_refresh(record(exp=NOW + 200), NOW, 0)              # Codex: 만료 5분 전
    assert ca.needs_refresh(record(access_token="not-a-jwt"), NOW, 0)   # 만료를 모르면 갱신


def test_record_from_tokens_rotates_and_takes_account_from_id_token():
    new = {"access_token": jwt({"exp": NOW + 7200}), "refresh_token": "rt-new",
           "id_token": id_token(account="acct-9999")}
    rec = ca.record_from_tokens(new, {**record(), ca.LEASE_FIELD: "123", "api_note": "kept"}, NOW)
    assert rec["refresh_token"] == "rt-new" and rec["account_id"] == "acct-9999"
    assert rec["expires_at"] == ca.iso(NOW + 7200) and rec["last_refresh"] == ca.iso(NOW)
    assert ca.LEASE_FIELD not in rec and rec["api_note"] == "kept" and rec["auth_mode"] == "chatgpt"


def test_refresh_request_matches_codex_source():
    calls = []

    def post(url, body):
        calls.append((url, body))
        return 200, {"access_token": "a2", "refresh_token": "r2", "id_token": "i2", "token_type": "Bearer"}

    assert ca.refresh_tokens("rt-old", post) == {"access_token": "a2", "refresh_token": "r2", "id_token": "i2"}
    assert calls == [("https://auth.openai.com/oauth/token",
                      {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "grant_type": "refresh_token",
                       "refresh_token": "rt-old"})]


@pytest.mark.parametrize("status,body,code", [
    (401, {}, "llm_auth_failed"),
    (400, {"error": {"code": "refresh_token_reused"}}, "llm_auth_failed"),
    (400, {"error": "refresh_token_expired"}, "llm_auth_failed"),
    (500, {}, "llm_unconfigured"),
])
def test_refresh_failures_are_classified_without_values(status, body, code):
    with pytest.raises(AgentRunError) as e:
        ca.refresh_tokens("rt-SECRET", lambda url, b: (status, body))
    assert e.value.code == code and "rt-SECRET" not in str(e.value)


def test_backend_headers_are_the_codex_account_headers():
    s = ca.session_from_record(record(id_token=id_token(fedramp=True)))
    h = ca.backend_headers(s, "sess-1")
    assert h["ChatGPT-Account-ID"] == "acct-1234" and h["session-id"] == "sess-1"
    assert h["X-OpenAI-Fedramp"] == "true" and "Authorization" not in h
    assert ca.CODEX_BASE_URL == "https://chatgpt.com/backend-api/codex"
