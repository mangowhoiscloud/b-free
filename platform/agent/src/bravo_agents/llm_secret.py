"""모델 자격을 실행 때 플랫폼 OpenBao에서 한 번 읽습니다(D-26: 키는 플랫폼 금고에만).

전달 방식은 deploy/pilot/scripts/keys.sh의 env-pipe와 같습니다.
- 플랫폼(리허설 서비스 또는 `keys.sh llm-run`)이 공급자 전용 AppRole(llm-claude, llm-openai)의 secret_id를
  한 번 쓰고 60초 지나면 사라지게 발급해 워커 환경에만 넣습니다(BRAVO_LLM_BAO_ROLE_ID, BRAVO_LLM_BAO_SECRET_ID).
- 워커는 그 secret_id로 로그인해(정책: 그 경로 하나 읽기만) 값을 읽고, 받은 토큰을 바로 폐기합니다.
- 값은 메모리에서 SDK에만 넘깁니다. 파일 · argv · 로그 · 예외 메시지에 넣지 않습니다.
값이 없으면 AgentRunError("llm_unconfigured")로 멈춥니다("토큰 미저장"). 가짜 자격을 만들지 않습니다.
"""
import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

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


def fetch_llm_secret(provider: str, environ: Mapping[str, str] | None = None) -> str:
    """공급자의 모델 자격 하나를 읽어 돌려줍니다. secret_id는 성공 · 실패와 상관없이 한 번만 씁니다."""
    env = dict(os.environ if environ is None else environ)
    if environ is None:
        os.environ.pop(SECRET_ID_ENV, None)    # 한 번 쓰는 값. 워커 환경에서 바로 지움
    spec = LLM_SECRETS.get(provider)
    if spec is None:
        raise AgentRunError("llm_unconfigured", f"{provider} takes no credential from OpenBao")
    problem = delivery_problem(provider, env)
    if problem:
        raise AgentRunError("llm_unconfigured", problem)
    addr = env[ADDR_ENV].rstrip("/")
    mount = (env.get(KV_MOUNT_ENV) or "secret").strip("/")
    cafile = (env.get(CACERT_ENV) or "").strip() or None
    try:
        ctx = ssl.create_default_context(cafile=cafile)
    except OSError as e:
        raise AgentRunError("llm_unconfigured", f"{CACERT_ENV} unreadable ({type(e).__name__})") from None
    status, login = _request("POST", f"{addr}/v1/auth/approle/login", ctx,
                             body={"role_id": env[ROLE_ID_ENV], "secret_id": env[SECRET_ID_ENV]})
    token = ((login.get("auth") or {}).get("client_token")) if status == 200 else None
    if not token:
        raise AgentRunError("llm_unconfigured", f"openbao approle login refused (HTTP {status})")
    try:
        status, doc = _request("GET", f"{addr}/v1/{mount}/data/{spec.path}", ctx, token=token)
    finally:
        _request("POST", f"{addr}/v1/auth/token/revoke-self", ctx, token=token)  # 받은 토큰은 바로 폐기
    if status == 404:
        raise AgentRunError("llm_unconfigured", f"secret/{spec.path} not stored")
    if status != 200:
        raise AgentRunError("llm_unconfigured", f"openbao read of secret/{spec.path} refused (HTTP {status})")
    value = ((doc.get("data") or {}).get("data") or {}).get(spec.field)
    if not isinstance(value, str) or not value.strip():
        raise AgentRunError("llm_unconfigured", f"secret/{spec.path} field {spec.field} not stored")
    return value.strip()
