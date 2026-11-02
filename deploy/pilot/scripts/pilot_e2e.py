"""Pilot end to end: operator MCP -> gate BLOCK -> human fix -> gate PASS -> console approval
-> console cutover under load -> receipt, write pause, zero loss.

Run (stack already up via compose.yaml):
    uv run --project platform/api python deploy/pilot/scripts/pilot_e2e.py
Everything is real: no mocks. Paths resolve relative to this file; URLs and project name come
from env (PLATFORM_MCP_URL, PLATFORM_API_URL, PILOT_COMPOSE_PROJECT).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

PILOT = Path(__file__).resolve().parents[1]
TOKENS = Path(os.environ.get("PLATFORM_DEVAUTH_OUT_DIR", PILOT / "devauth")) / "tokens"
MCP_URL = os.environ.get("PLATFORM_MCP_URL", "http://localhost:8082/mcp")
API_URL = os.environ.get("PLATFORM_API_URL", "http://localhost:8080")
PROJECT = os.environ.get("PILOT_COMPOSE_PROJECT", "bravo-pilot")
COMPOSE = ["docker", "compose", "-p", PROJECT, "--env-file", str(PILOT / ".env"), "-f", str(PILOT / "compose.yaml")]
RUN = uuid.uuid4().hex[:8]


def tok(user: str, aud: str) -> str:
    return (TOKENS / f"{user}.{aud}.jwt").read_text().strip()


def step(title: str, value: Any = None) -> None:
    print(f"\n== {title}", flush=True)
    if value is not None:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str)[:3000], flush=True)


def rest(method: str, path: str, user: str, body: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
    headers = {"Cf-Access-Jwt-Assertion": tok(user, "console-aud"), "Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


async def mcp(name: str, args: dict[str, Any]) -> dict[str, Any]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok('operator', 'mcp-aud')}"}, timeout=60)
    async with http, Client(streamable_http_client(MCP_URL, http_client=http)) as client:
        result = await client.call_tool(name, args)
    if result.is_error:
        raise SystemExit(f"MCP {name} returned isError: {result.structured_content}")
    return result.structured_content or {}


def fix_planted_regression() -> None:
    sql = (PILOT / "fix-dst-grant.sql").read_bytes()
    subprocess.run(
        [*COMPOSE, "exec", "-T", "dst-db", "sh", "-c",
         'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres -d "$POSTGRES_DB"'],
        input=sql, check=True,
    )


async def main() -> int:
    step("1. 운영자 MCP: 환경 등록과 계획 생성")
    src = await mcp("env_register", {"name": f"chat-aws-{RUN}", "role": "source",
                                     "secret_ref": "tenants/t1/chat/src-db", "idempotency_key": f"src-{RUN}"})
    dst = await mcp("env_register", {"name": f"chat-onprem-{RUN}", "role": "target",
                                     "secret_ref": "tenants/t1/chat/dst-db", "idempotency_key": f"dst-{RUN}"})
    plan = await mcp("plan_create", {"workload": "chat", "source_environment_id": src["environment_id"],
                                     "target_environment_id": dst["environment_id"], "idempotency_key": f"plan-{RUN}"})
    plan_id = plan["plan_id"]
    step("plan", plan)

    step("2. 운영자 MCP: 게이트 1차(심은 퇴행이 있어야 BLOCK)")
    gate1 = await mcp("gate_run", {"plan_id": plan_id, "idempotency_key": f"gate1-{RUN}"})
    step("gate 1", gate1)
    if gate1.get("verdict") != "BLOCK":
        print("기대: BLOCK"); return 1

    step("3. 사람이 목적지 권한을 고침(fix-dst-grant.sql)")
    fix_planted_regression()

    step("4. 운영자 MCP: 게이트 재대조")
    gate2 = await mcp("gate_run", {"plan_id": plan_id, "idempotency_key": f"gate2-{RUN}"})
    step("gate 2", gate2)
    if gate2.get("verdict") != "PASS":
        print("기대: PASS"); return 1

    step("5. 승인자: 콘솔에서 승인(MCP에는 승인 도구가 없음)")
    awaiting = rest("GET", f"/v1/plans/{plan_id}", "approver")["awaiting_approval"]
    approval = rest("POST", f"/v1/plans/{plan_id}/approvals", "approver",
                    {"stage": "cutover", "approval_request_id": awaiting["approval_request_id"],
                     "revision": awaiting["revision"], "gate_verdict_id": awaiting.get("gate_verdict_id")},
                    key=f"approve-{RUN}")
    step("approval", approval)

    step("6. 부하 시작(PgBouncer로 채팅 쓰기, 멱등 키)")
    load = subprocess.Popen([*COMPOSE, "--profile", "load", "run", "--rm", "-T", "-e", "LOADGEN_DURATION_S=40", "loadgen"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(10)

    step("7. 운영자: 콘솔에서 컷오버 시작")
    shift = rest("POST", f"/v1/plans/{plan_id}/shift", "operator", {"stage": "cutover"}, key=f"shift-{RUN}")
    step("shift", shift)

    final: dict[str, Any] = {}
    for _ in range(120):
        final = rest("GET", f"/v1/plans/{plan_id}", "operator")
        if final.get("state") in {"WATCH", "SAFE_HOLD", "AWAIT_APPROVAL"} and final.get("last_run", {}).get("status") != "RUNNING":
            break
        time.sleep(1)
    step("8. 결과(plan_get)", {k: final.get(k) for k in ("state", "last_run", "last_receipt")})

    out, _ = load.communicate(timeout=180)
    summary = next((l for l in reversed(out.splitlines()) if l.strip().startswith("{")), "")
    step("9. 부하 생성기 요약", json.loads(summary) if summary else out[-1500:])

    events = await mcp("plan_events", {"plan_id": plan_id, "after": 0, "limit": 200})
    step("10. 이벤트(종류 · 상태 · 요청 경로)",
         [(e.get("kind"), e.get("state"), (e.get("actor") or {}).get("kind")) for e in events.get("events", [])])
    return 0 if final.get("state") == "WATCH" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
