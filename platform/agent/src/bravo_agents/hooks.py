"""SDK hook. 기록과 상태 덧붙이기만 합니다. 허용 · 거부는 서버 쪽 도구 계층이 합니다(Pod 안 hook은 우회 가능)."""
import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from itertools import count
from typing import Any, Awaitable, Callable, Protocol

from bravo_agents.envelope import StateEnvelope, envelope_digest, remaining_seconds
from bravo_agents.errors import AgentRunError

EVIDENCE_URL_ENV = "BRAVO_EVIDENCE_URL"
log = logging.getLogger(__name__)


class EvidenceSink(Protocol):
    async def emit(self, event: dict) -> None: ...


class HttpEvidenceSink:
    """리허설 서비스 증거 수신 API로 사건을 하나씩 보냅니다. ACK가 없으면 같은 seq로 다시 보냅니다.

    이 기록은 권위가 없습니다. 권위 있는 기록은 도구 계층이 직접 플랫폼 DB에 씁니다.
    """

    def __init__(self, url: str, trial_token: str, retries: int = 3, timeout_s: float = 5.0):
        self.url, self.token, self.retries, self.timeout_s = url, trial_token, retries, timeout_s

    def _post(self, body: bytes) -> None:
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 (주소는 플랫폼 설정)
            if resp.status not in (200, 201, 202, 204, 409):  # 409 = 이미 받은 seq
                raise OSError(f"evidence status {resp.status}")

    async def emit(self, event: dict) -> None:
        body = json.dumps(event, sort_keys=True, default=str).encode()
        for i in range(self.retries):
            try:
                await asyncio.to_thread(self._post, body)
                return
            except OSError as e:
                log.warning("evidence emit failed seq=%s try=%d: %s", event.get("seq"), i + 1, e)
        # 증거 전송 실패는 Trial을 멈추지 않습니다. 받는 쪽이 seq 빈 곳을 "누락"으로 표시합니다.


def evidence_sink_from_env(trial_token: str) -> HttpEvidenceSink:
    url = os.environ.get(EVIDENCE_URL_ENV, "")
    if not url.startswith(("https://", "http://")):
        raise AgentRunError("evidence_unconfigured", f"{EVIDENCE_URL_ENV} not set")
    return HttpEvidenceSink(url, trial_token)


Hook = Callable[[dict, str | None, Any], Awaitable[dict]]


def make_hooks(envelope: StateEnvelope, sink: EvidenceSink,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, Hook]:
    """PreToolUse(기록만), PostToolUse(기록 + 상태 변화), UserPromptSubmit(상태 요약)을 만듭니다."""
    seq = count(1)
    base = {"trial_id": envelope.trial_id, "plan_id": envelope.plan_id, "attempt": envelope.attempt,
            "role": envelope.role}

    def delta() -> str:
        return (f"[platform] attempt {envelope.attempt}/{envelope.max_attempts}, "
                f"{remaining_seconds(envelope, clock())}s left before deadline, "
                f"state {envelope.state}, envelope {envelope_digest(envelope)}")

    async def pre_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        await sink.emit({**base, "seq": next(seq), "kind": "tool_request", "tool_use_id": tool_use_id,
                         "tool_name": input_data.get("tool_name"), "tool_input": input_data.get("tool_input"),
                         "at": clock().isoformat()})
        return {}  # 결정하지 않음. 허용은 allowed_tools, 나머지는 dontAsk가 거부, 집행은 서버

    async def post_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        await sink.emit({**base, "seq": next(seq), "kind": "tool_result", "tool_use_id": tool_use_id,
                         "tool_name": input_data.get("tool_name"), "at": clock().isoformat()})
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": delta()}}

    async def user_prompt_submit(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": delta()}}

    return {"PreToolUse": pre_tool_use, "PostToolUse": post_tool_use, "UserPromptSubmit": user_prompt_submit}
