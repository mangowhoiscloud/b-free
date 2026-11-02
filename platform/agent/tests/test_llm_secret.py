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
