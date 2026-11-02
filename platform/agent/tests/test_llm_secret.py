"""한 번 쓰는 AppRole 읽기(llm_secret.fetch_llm_secret). OpenBao HTTP는 가짜로 바꾸고, 값이 오류 · 로그에 없는지 봅니다."""
import pytest

from bravo_agents import llm_secret
from bravo_agents.errors import AgentRunError

ENV = {"BRAVO_BAO_ADDR": "https://127.0.0.1:8200", "BRAVO_LLM_BAO_ROLE_ID": "role-1",
       "BRAVO_LLM_BAO_SECRET_ID": "sid-1"}


class FakeBao:
    def __init__(self, read_status=200, data=None, login_status=200):
        self.calls, self.read_status, self.login_status = [], read_status, login_status
        self.data = {"oauth_token": "sk-ant-oat01-SECRET"} if data is None else data

    def __call__(self, method, url, ctx, token=None, body=None):
        self.calls.append((method, url.split("/v1/", 1)[1], token, body))
        if url.endswith("/auth/approle/login"):
            return self.login_status, ({"auth": {"client_token": "s.short"}} if self.login_status == 200 else {})
        if url.endswith("/auth/token/revoke-self"):
            return 204, {}
        return self.read_status, ({"data": {"data": self.data}} if self.read_status == 200 else {})


def test_reads_one_field_with_one_use_approle_and_revokes(monkeypatch):
    bao = FakeBao()
    monkeypatch.setattr(llm_secret, "_request", bao)
    assert llm_secret.fetch_llm_secret("claude_subscription", ENV) == "sk-ant-oat01-SECRET"
    assert [c[:2] for c in bao.calls] == [("POST", "auth/approle/login"),
                                          ("GET", "secret/data/envs/platform/llm-claude"),
                                          ("POST", "auth/token/revoke-self")]
    assert bao.calls[0][3] == {"role_id": "role-1", "secret_id": "sid-1"}
    assert bao.calls[1][2] == bao.calls[2][2] == "s.short"


def test_openai_key_path_and_mount(monkeypatch):
    bao = FakeBao(data={"api_key": "sk-proj-SECRET"})
    monkeypatch.setattr(llm_secret, "_request", bao)
    assert llm_secret.fetch_llm_secret("openai_agents", {**ENV, "BRAVO_BAO_KV_MOUNT": "kv"}) == "sk-proj-SECRET"
    assert bao.calls[1][1] == "kv/data/envs/platform/llm-openai"


@pytest.mark.parametrize("bao,detail", [
    (FakeBao(read_status=404), "secret/envs/platform/llm-claude not stored"),
    (FakeBao(data={"other": "x"}), "field oauth_token not stored"),
    (FakeBao(data={"oauth_token": " "}), "field oauth_token not stored"),
    (FakeBao(read_status=403), "refused (HTTP 403)"),
    (FakeBao(login_status=400), "login refused (HTTP 400)"),
])
def test_absent_or_refused_fails_closed(monkeypatch, bao, detail):
    monkeypatch.setattr(llm_secret, "_request", bao)
    with pytest.raises(AgentRunError) as e:
        llm_secret.fetch_llm_secret("claude_subscription", ENV)
    assert e.value.code == "llm_unconfigured" and e.value.infra and detail in e.value.detail
    assert "SECRET" not in str(e.value)
    if bao.login_status == 200:
        assert bao.calls[-1][1] == "auth/token/revoke-self"     # 실패해도 토큰은 폐기


@pytest.mark.parametrize("drop", ["BRAVO_BAO_ADDR", "BRAVO_LLM_BAO_ROLE_ID", "BRAVO_LLM_BAO_SECRET_ID"])
def test_missing_delivery_fails_closed_without_network(monkeypatch, drop):
    def no_network(*a, **k):
        raise AssertionError("no request expected")
    monkeypatch.setattr(llm_secret, "_request", no_network)
    env = {k: v for k, v in ENV.items() if k != drop}
    assert drop in llm_secret.delivery_problem("claude_subscription", env)
    with pytest.raises(AgentRunError) as e:
        llm_secret.fetch_llm_secret("claude_subscription", env)
    assert e.value.code == "llm_unconfigured"


def test_plain_http_and_gateway_providers_are_refused(monkeypatch):
    assert "https" in llm_secret.delivery_problem("claude_subscription", {**ENV, "BRAVO_BAO_ADDR": "http://bao:8200"})
    assert llm_secret.delivery_problem("bedrock_gateway", {}) is None
    with pytest.raises(AgentRunError):
        llm_secret.fetch_llm_secret("bedrock_gateway", ENV)


def test_secret_id_is_dropped_from_process_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(llm_secret, "_request", FakeBao())
    import os
    assert llm_secret.fetch_llm_secret("claude_subscription") == "sk-ant-oat01-SECRET"
    assert "BRAVO_LLM_BAO_SECRET_ID" not in os.environ


# --- openai_agents: api_key 또는 ChatGPT 세션(갱신 + CAS 되쓰기) ---------------------------------------------
from test_chatgpt_auth import NOW, id_token, jwt, record  # noqa: E402

from bravo_agents import chatgpt_auth  # noqa: E402


class KvBao:
    """KV v2 한 경로: 판 번호와 check-and-set을 흉내 냅니다."""

    def __init__(self, data, version=3, write_status=None):
        self.data, self.version, self.calls, self.write_status = dict(data), version, [], write_status

    def __call__(self, method, url, ctx, token=None, body=None):
        self.calls.append((method, url.split("/v1/", 1)[1], token, body))
        if url.endswith("/auth/approle/login"):
            return 200, {"auth": {"client_token": "s.short"}}
        if url.endswith("/auth/token/revoke-self"):
            return 204, {}
        if method == "GET":
            return 200, {"data": {"data": dict(self.data), "metadata": {"version": self.version}}}
        if self.write_status:
            return self.write_status, {}
        if body["options"]["cas"] != self.version:
            return 400, {}
        self.data, self.version = dict(body["data"]), self.version + 1
        return 200, {"data": {"version": self.version}}

    def writes(self):
        return [c[3]["data"] for c in self.calls if c[0] == "POST" and c[1].startswith("secret/data/")]


def test_openai_api_key_wins_over_session(monkeypatch):
    bao = KvBao({**record(), "api_key": "sk-proj-SECRET"})
    monkeypatch.setattr(llm_secret, "_request", bao)
    cred = llm_secret.fetch_openai_credential(ENV, now=lambda: NOW)
    assert (cred.mode, cred.api_key, cred.session) == ("api_key", "sk-proj-SECRET", None)
    assert bao.writes() == [] and bao.calls[-1][1] == "auth/token/revoke-self"


def test_valid_chatgpt_session_is_used_without_refresh(monkeypatch):
    bao = KvBao(record(exp=NOW + 7200))
    monkeypatch.setattr(llm_secret, "_request", bao)
    cred = llm_secret.fetch_openai_credential(
        ENV, min_valid_s=900, now=lambda: NOW, refresh=lambda rt: pytest.fail("no refresh expected"))
    assert cred.mode == "chatgpt" and not cred.refreshed and cred.session.account_id == "acct-1234"
    assert "SECRET" not in repr(cred) and cred.session.access_token not in repr(cred)
    assert bao.writes() == []


def test_expiring_session_is_refreshed_and_written_back_with_cas(monkeypatch):
    bao = KvBao(record(exp=NOW + 120), version=3)
    monkeypatch.setattr(llm_secret, "_request", bao)
    seen = []

    def refresh(rt):
        seen.append((rt, bao.data.get(chatgpt_auth.LEASE_FIELD)))   # 갱신 때 임대가 잡혀 있어야 함
        return {"access_token": jwt({"exp": NOW + 36000}), "refresh_token": "rt-new", "id_token": id_token()}

    cred = llm_secret.fetch_openai_credential(ENV, min_valid_s=900, now=lambda: NOW, refresh=refresh)
    assert cred.refreshed and cred.session.expires_at == NOW + 36000
    assert seen == [("rt-old", str(NOW + llm_secret.LEASE_S))]
    lease, final = bao.writes()
    assert lease[chatgpt_auth.LEASE_FIELD] and lease["refresh_token"] == "rt-old"
    assert final["refresh_token"] == "rt-new" and chatgpt_auth.LEASE_FIELD not in final
    assert [c[3]["options"]["cas"] for c in bao.calls if c[0] == "POST" and "data/envs" in c[1]] == [3, 4]
    assert bao.version == 5 and bao.calls[-1][1] == "auth/token/revoke-self"


def test_session_refreshed_by_another_worker_is_reused(monkeypatch):
    bao = KvBao({**record(exp=NOW + 120), chatgpt_auth.LEASE_FIELD: str(NOW + 30)})
    monkeypatch.setattr(llm_secret, "_request", bao)

    def other_worker_finishes(_s):
        bao.data, bao.version = record(exp=NOW + 36000, refresh_token="rt-other"), bao.version + 1

    cred = llm_secret.fetch_openai_credential(ENV, now=lambda: NOW, sleep=other_worker_finishes,
                                              refresh=lambda rt: pytest.fail("must not reuse the refresh token"))
    assert cred.mode == "chatgpt" and not cred.refreshed and cred.session.expires_at == NOW + 36000
    assert bao.writes() == []


def test_refresh_failure_releases_lease_and_keeps_old_tokens(monkeypatch):
    bao = KvBao(record(exp=NOW + 120))
    monkeypatch.setattr(llm_secret, "_request", bao)

    def refused(rt):
        raise AgentRunError("llm_auth_failed", "ChatGPT session cannot be refreshed (refresh_token_expired)")

    with pytest.raises(AgentRunError) as e:
        llm_secret.fetch_openai_credential(ENV, now=lambda: NOW, refresh=refused)
    assert e.value.code == "llm_auth_failed"
    assert chatgpt_auth.LEASE_FIELD not in bao.data and bao.data["refresh_token"] == "rt-old"
    assert bao.calls[-1][1] == "auth/token/revoke-self"


@pytest.mark.parametrize("data", [{}, {"auth_mode": "chatgpt", "access_token": "x"}, {"api_key": " "}])
def test_openai_record_without_key_or_session_fails_closed(monkeypatch, data):
    monkeypatch.setattr(llm_secret, "_request", KvBao(data))
    with pytest.raises(AgentRunError) as e:
        llm_secret.fetch_openai_credential(ENV, now=lambda: NOW)
    assert e.value.code == "llm_unconfigured"
