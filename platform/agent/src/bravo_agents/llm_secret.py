"""모델 자격을 실행 때 플랫폼 OpenBao에서 한 번 읽습니다(D-26: 키는 플랫폼 금고에만).

전달 방식은 deploy/pilot/scripts/keys.sh의 env-pipe와 같습니다.
- 플랫폼(리허설 서비스 또는 `keys.sh llm-run`)이 공급자 전용 AppRole(llm-claude, llm-openai)의 secret_id를
  한 번 쓰고 60초 지나면 사라지게 발급해 워커 환경에만 넣습니다(BRAVO_LLM_BAO_ROLE_ID, BRAVO_LLM_BAO_SECRET_ID).
- 워커는 그 secret_id로 로그인해(정책: 그 경로 하나 읽기만) 값을 읽고, 받은 토큰을 바로 폐기합니다.
- 값은 메모리에서 SDK에만 넘깁니다. 파일 · argv · 로그 · 예외 메시지에 넣지 않습니다.
값이 없으면 AgentRunError("llm_unconfigured")로 멈춥니다("토큰 미저장"). 가짜 자격을 만들지 않습니다.

openai_agents는 레코드 하나에 두 모드가 있습니다(fetch_openai_credential).
- `api_key`가 있으면 그것을 씁니다(운영 대안: 플랫폼이 쥔 OpenAI API 키).
- 없고 `auth_mode=chatgpt`이면 ChatGPT 계정 세션(데모 전용, chatgpt_auth.py). 접근 토큰이 실행 시간 안에 끝나면
  Codex와 같은 토큰 주소로 갱신하고, 회전된 토큰을 KV v2 check-and-set으로 같은 경로에 다시 씁니다
  (AppRole llm-openai의 두 번째 정책 platform-llm-openai-refresh: 그 경로 update만). 갱신 전에 짧은 임대를
  CAS로 잡아 두 워커가 같은 refresh_token을 두 번 쓰지 않게 합니다(두 번 쓰면 세션이 끊김).
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlparse

from bravo_agents import chatgpt_auth
from bravo_agents.errors import AgentRunError
from bravo_agents.llm import OPENAI, SUBSCRIPTION

ADDR_ENV = "BRAVO_BAO_ADDR"
CACERT_ENV = "BRAVO_BAO_CACERT"
KV_MOUNT_ENV = "BRAVO_BAO_KV_MOUNT"
ROLE_ID_ENV = "BRAVO_LLM_BAO_ROLE_ID"
SECRET_ID_ENV = "BRAVO_LLM_BAO_SECRET_ID"
DELIVERY_VARS = (ADDR_ENV, CACERT_ENV, KV_MOUNT_ENV, ROLE_ID_ENV, SECRET_ID_ENV)


@dataclass(frozen=True)
class LlmSecret:
    path: str      # KV v2 경로(마운트 아래)
    field: str
    approle: str   # 이 경로 하나만 읽는 AppRole(정책 deploy/pilot/bao/policies/platform-<approle>.hcl)


LLM_SECRETS = {
    SUBSCRIPTION: LlmSecret("envs/platform/llm-claude", "oauth_token", "llm-claude"),
    OPENAI: LlmSecret("envs/platform/llm-openai", "api_key", "llm-openai"),
}


def delivery_problem(provider: str, environ: Mapping[str, str] | None = None) -> str | None:
    """워커가 자격을 받을 수 있는지(값은 보지 않음). 문제가 있으면 이유 문자열."""
    env = os.environ if environ is None else environ
    if provider not in LLM_SECRETS:
        return None
    missing = [k for k in (ADDR_ENV, ROLE_ID_ENV, SECRET_ID_ENV) if not (env.get(k) or "").strip()]
    if missing:
        return f"openbao delivery not configured: {','.join(missing)} not set"
    if urlparse(env[ADDR_ENV]).scheme != "https":
        return f"{ADDR_ENV} must be https"
    return None


def _request(method: str, url: str, ctx: ssl.SSLContext, token: str | None = None,
             body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # noqa: S310 (플랫폼 설정 주소)
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AgentRunError("llm_unconfigured", f"openbao unreachable ({type(e).__name__})") from None


class _Bao:
    """한 번 쓰는 secret_id로 로그인한 짧은 토큰 하나. 끝나면 반드시 close()로 폐기합니다."""

    def __init__(self, provider: str, environ: Mapping[str, str] | None):
        env = dict(os.environ if environ is None else environ)
        if environ is None:
            os.environ.pop(SECRET_ID_ENV, None)    # 한 번 쓰는 값. 워커 환경에서 바로 지움
        spec = LLM_SECRETS.get(provider)
        if spec is None:
            raise AgentRunError("llm_unconfigured", f"{provider} takes no credential from OpenBao")
        problem = delivery_problem(provider, env)
        if problem:
            raise AgentRunError("llm_unconfigured", problem)
        self.spec = spec
        self.addr = env[ADDR_ENV].rstrip("/")
        self.mount = (env.get(KV_MOUNT_ENV) or "secret").strip("/")
        cafile = (env.get(CACERT_ENV) or "").strip() or None
        try:
            self.ctx = ssl.create_default_context(cafile=cafile)
        except OSError as e:
            raise AgentRunError("llm_unconfigured", f"{CACERT_ENV} unreadable ({type(e).__name__})") from None
        status, login = _request("POST", f"{self.addr}/v1/auth/approle/login", self.ctx,
                                 body={"role_id": env[ROLE_ID_ENV], "secret_id": env[SECRET_ID_ENV]})
        self.token = ((login.get("auth") or {}).get("client_token")) if status == 200 else None
        if not self.token:
            raise AgentRunError("llm_unconfigured", f"openbao approle login refused (HTTP {status})")

    @property
    def data_url(self) -> str:
        return f"{self.addr}/v1/{self.mount}/data/{self.spec.path}"

    def read(self) -> tuple[dict, int]:
        """(필드, KV 판). 없거나 거부면 llm_unconfigured."""
        status, doc = _request("GET", self.data_url, self.ctx, token=self.token)
        if status == 404:
            raise AgentRunError("llm_unconfigured", f"secret/{self.spec.path} not stored")
        if status != 200:
            raise AgentRunError("llm_unconfigured", f"openbao read of secret/{self.spec.path} refused (HTTP {status})")
        body = doc.get("data") or {}
        return dict(body.get("data") or {}), int((body.get("metadata") or {}).get("version") or 0)

    def write_cas(self, data: dict, version: int) -> int | None:
        """KV v2 check-and-set. 새 판, 판이 달라졌으면 None(다른 워커가 먼저 씀)."""
        status, doc = _request("POST", self.data_url, self.ctx, token=self.token,
                               body={"options": {"cas": version}, "data": data})
        if status in (200, 204):
            return int(((doc.get("data") or {}).get("version")) or version + 1)
        if status == 400:
            return None
        raise AgentRunError("llm_unconfigured", f"openbao write of secret/{self.spec.path} refused (HTTP {status})")

    def close(self) -> None:
        if self.token:
            _request("POST", f"{self.addr}/v1/auth/token/revoke-self", self.ctx, token=self.token)  # 받은 토큰은 바로 폐기
            self.token = None


def fetch_llm_secret(provider: str, environ: Mapping[str, str] | None = None) -> str:
    """공급자의 모델 자격 하나를 읽어 돌려줍니다. secret_id는 성공 · 실패와 상관없이 한 번만 씁니다."""
    bao = _Bao(provider, environ)
    try:
        data, _ = bao.read()
    finally:
        bao.close()
    value = data.get(bao.spec.field)
    if not isinstance(value, str) or not value.strip():
        raise AgentRunError("llm_unconfigured", f"secret/{bao.spec.path} field {bao.spec.field} not stored")
    return value.strip()


@dataclass(frozen=True)
class OpenAICredential:
    """openai_agents의 자격. mode="api_key"(운영 대안) 또는 "chatgpt"(데모, Codex 백엔드)."""
    mode: str
    api_key: str = ""
    session: chatgpt_auth.ChatGPTSession | None = None
    refreshed: bool = False

    def __repr__(self) -> str:   # 값을 repr에 넣지 않음
        return f"OpenAICredential(mode={self.mode}, session={self.session!r}, refreshed={self.refreshed})"


LEASE_S = 60          # 갱신 임대. 토큰 주소 호출 한 번이면 충분
LEASE_WAIT_S = 30     # 다른 워커의 임대를 기다리는 최대 시간


def _refresh_and_store(bao: _Bao, data: dict, version: int, min_valid_s: float,
                       refresh: Callable[[str], dict], now: Callable[[], float],
                       sleep: Callable[[float], None]) -> tuple[dict, bool]:
    """(쓸 레코드, 이번 실행이 갱신했는지). 임대 CAS → 갱신 → 새 토큰 CAS. 다른 워커가 갱신했으면 그 값을 씀."""
    deadline = now() + LEASE_WAIT_S
    while True:
        if not chatgpt_auth.needs_refresh(data, now(), min_valid_s):
            return data, False                                    # 다른 워커가 이미 갱신
        lease = data.get(chatgpt_auth.LEASE_FIELD)
        try:
            leased = float(lease) > now() if lease else False
        except (TypeError, ValueError):
            leased = False
        if not leased:
            claimed = bao.write_cas({**data, chatgpt_auth.LEASE_FIELD: str(int(now() + LEASE_S))}, version)
            if claimed is not None:
                version = claimed
                break
        if now() >= deadline:
            raise AgentRunError("llm_unconfigured", "ChatGPT session refresh held by another worker")
        sleep(2.0)
        data, version = bao.read()
    try:
        tokens = refresh(data["refresh_token"])
    except AgentRunError:
        released = {k: v for k, v in data.items() if k != chatgpt_auth.LEASE_FIELD}
        try:
            bao.write_cas(released, version)                      # 임대를 풀고 이전 토큰을 그대로 둠
        except AgentRunError:
            pass                                                  # 임대는 LEASE_S 뒤 저절로 풀림
        raise
    fresh = chatgpt_auth.record_from_tokens(tokens, data, now())
    for _ in range(3):   # 회전된 refresh_token은 여기 말고는 없음. 쓰기를 몇 번 다시 시도
        if bao.write_cas(fresh, version) is not None:
            return fresh, True
        sleep(0.5)
    raise AgentRunError("llm_unconfigured", "refreshed ChatGPT session could not be written back (CAS conflict)")


def fetch_openai_credential(environ: Mapping[str, str] | None = None, *, min_valid_s: float = 900,
                            refresh: Callable[[str], dict] | None = None, now: Callable[[], float] = time.time,
                            sleep: Callable[[float], None] = time.sleep) -> OpenAICredential:
    """openai_agents의 자격 하나. api_key가 있으면 그것, 없으면 ChatGPT 세션(필요하면 갱신 후 되씀)."""
    bao = _Bao(OPENAI, environ)
    try:
        data, version = bao.read()
        key = data.get("api_key")
        if isinstance(key, str) and key.strip():
            return OpenAICredential("api_key", api_key=key.strip())
        chatgpt_auth.session_from_record(data)                    # 모양 확인(빠지면 llm_unconfigured)
        refreshed = False
        if chatgpt_auth.needs_refresh(data, now(), min_valid_s):
            data, refreshed = _refresh_and_store(bao, data, version, min_valid_s,
                                                 refresh or chatgpt_auth.refresh_tokens, now, sleep)
        return OpenAICredential("chatgpt", session=chatgpt_auth.session_from_record(data), refreshed=refreshed)
    finally:
        bao.close()
