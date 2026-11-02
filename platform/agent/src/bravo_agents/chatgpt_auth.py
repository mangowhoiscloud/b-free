"""openai_agents의 ChatGPT 계정 모드(데모 전용): Codex CLI와 같은 로그인 세션으로 ChatGPT Codex 백엔드를 부릅니다.

OpenAI는 Agents SDK에 "ChatGPT로 로그인"을 문서화하지 않았습니다. 이 모드는 Codex CLI(Apache-2.0,
github.com/openai/codex)와 같은 OAuth 클라이언트 · 토큰 주소 · 백엔드를 씁니다. 근거는 그 소스입니다.
- 로그인 · 갱신: codex-rs/login/src/server.rs, auth/manager.rs(`CLIENT_ID`, `REFRESH_TOKEN_URL`,
  갱신 요청 {client_id, grant_type: refresh_token, refresh_token}, 만료 5분 전 갱신)
- 백엔드: codex-rs/model-provider-info/src/lib.rs `CHATGPT_CODEX_BASE_URL`(Responses 호환, `/responses`)
- 헤더: codex-rs/model-provider/src/bearer_auth_provider.rs(`Authorization`, `ChatGPT-Account-ID`,
  `X-OpenAI-Fedramp`), codex-rs/codex-api/src/requests/headers.rs(`session-id`)
- 요청: codex-rs/core/src/client.rs build_responses_request(`store=false`, `stream=true`,
  `include=["reasoning.encrypted_content"]`, `instructions`)
운영 경로가 아닙니다. 운영은 플랫폼이 쥔 OpenAI API 키(`api_key`) 또는 게이트웨이입니다(13-agents.md 6절, ADR 0009).
세션은 플랫폼 OpenBao `secret/envs/platform/llm-openai`에만 있습니다(D-26). 이 모듈은 값을 로그 · 예외에 넣지 않습니다.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from bravo_agents.errors import AgentRunError

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"     # Codex CLI의 공개 OAuth 클라이언트(PKCE, 비밀 없음)
AUTH_MODE = "chatgpt"
SESSION_FIELDS = ("access_token", "refresh_token", "id_token", "account_id")
REFRESH_MARGIN_S = 300                           # Codex: 만료 5분 전이면 갱신
LEASE_FIELD = "refresh_lease_until"              # 갱신 중인 워커가 잡는 짧은 임대(초, epoch). 동시 갱신 막기
USER_AGENT = "bravo-agents/0.1.0"
_PERMANENT = ("refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated")


def jwt_claims(token: str) -> dict[str, Any]:
    """서명은 보지 않고 payload만 읽습니다(만료 · 계정 ID 확인용). 형식이 틀리면 빈 dict."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or not all(parts):
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def expires_at_of(access_token: str) -> int | None:
    exp = jwt_claims(access_token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def iso(epoch: int | float | None) -> str:
    return "" if epoch is None else datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auth_claims(id_token: str) -> dict[str, Any]:
    auth = jwt_claims(id_token).get("https://api.openai.com/auth")
    return auth if isinstance(auth, dict) else {}


@dataclass(frozen=True)
class ChatGPTSession:
    access_token: str
    account_id: str
    expires_at: int | None
    fedramp: bool = False
    plan: str | None = None

    def __repr__(self) -> str:   # 토큰을 repr에 넣지 않음
        return f"ChatGPTSession(account=…{self.account_id[-4:]}, expires_at={iso(self.expires_at)}, plan={self.plan})"


def session_from_record(data: Mapping[str, Any]) -> ChatGPTSession:
    """OpenBao 레코드에서 세션을 만듭니다. 필드가 빠지면 llm_unconfigured."""
    missing = [f for f in SESSION_FIELDS if not isinstance(data.get(f), str) or not data[f].strip()]
    if data.get("auth_mode") != AUTH_MODE or missing:
        raise AgentRunError("llm_unconfigured", "secret/envs/platform/llm-openai holds neither api_key nor a complete "
                                                f"ChatGPT session (missing {','.join(missing) or 'auth_mode'})")
    auth = _auth_claims(data["id_token"])
    return ChatGPTSession(access_token=data["access_token"].strip(), account_id=data["account_id"].strip(),
                          expires_at=expires_at_of(data["access_token"]),
                          fedramp=bool(auth.get("chatgpt_account_is_fedramp")),
                          plan=auth.get("chatgpt_plan_type") if isinstance(auth.get("chatgpt_plan_type"), str) else None)


def needs_refresh(data: Mapping[str, Any], now: float, min_valid_s: float) -> bool:
    """접근 토큰이 now + min_valid_s 안에 끝나면 갱신. 만료를 읽지 못하면 갱신."""
    exp = expires_at_of(str(data.get("access_token") or ""))
    return exp is None or exp <= now + max(min_valid_s, REFRESH_MARGIN_S)


def record_from_tokens(tokens: Mapping[str, Any], base: Mapping[str, Any] | None = None,
                       now: float | None = None) -> dict[str, str]:
    """로그인 · 갱신 응답을 OpenBao 레코드로. account_id는 id_token 클레임이 우선(Codex TokenData와 같음)."""
    rec = {k: v for k, v in dict(base or {}).items() if k != LEASE_FIELD}
    for f in ("access_token", "refresh_token", "id_token"):
        if isinstance(tokens.get(f), str) and tokens[f]:
            rec[f] = tokens[f]
    account = _auth_claims(rec.get("id_token", "")).get("chatgpt_account_id") or tokens.get("account_id")
    if isinstance(account, str) and account:
        rec["account_id"] = account
    rec["auth_mode"] = AUTH_MODE
    rec["expires_at"] = iso(expires_at_of(rec.get("access_token", "")))
    if now is not None:
        rec["last_refresh"] = iso(now)
    return rec


def backend_headers(session: ChatGPTSession, session_id: str) -> dict[str, str]:
    """Codex 소스가 백엔드에 보내는 계정 헤더. Authorization은 AsyncOpenAI(api_key=access_token)가 붙입니다."""
    headers = {"ChatGPT-Account-ID": session.account_id, "session-id": session_id, "User-Agent": USER_AGENT}
    if session.fedramp:
        headers["X-OpenAI-Fedramp"] = "true"
    return headers


def _post_json(url: str, body: dict, timeout: float = 20.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:  # noqa: S310
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AgentRunError("llm_unconfigured", f"chatgpt token endpoint unreachable ({type(e).__name__})") from None


def _error_code(body: Mapping[str, Any]) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or "")
    return str(err or body.get("code") or "")


def refresh_tokens(refresh_token: str, post: Callable[[str, dict], tuple[int, dict]] = _post_json) -> dict[str, str]:
    """Codex와 같은 갱신 요청. 새 refresh_token이 오면 이전 것은 다시 못 씁니다(회전)."""
    status, body = post(TOKEN_URL, {"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                    "refresh_token": refresh_token})
    if status == 200 and isinstance(body.get("access_token"), str):
        return {k: body[k] for k in ("access_token", "refresh_token", "id_token") if isinstance(body.get(k), str)}
    code = _error_code(body).lower()
    if status == 401 or code in _PERMANENT:
        raise AgentRunError("llm_auth_failed", f"ChatGPT session cannot be refreshed ({code or status}); "
                                               "log in again: keys.sh llm-login openai")
    raise AgentRunError("llm_unconfigured", f"ChatGPT token refresh failed (HTTP {status} {code})".strip())
