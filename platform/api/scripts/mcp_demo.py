"""Operator MCP walkthrough with the official SDK client over Streamable HTTP + Bearer JWT.

tools/list (operator vs viewer) -> env_register x2 -> plan_create -> plan_get -> gate_run
-> plan_events, plus the 403 split (viewer calling gate_run) and an isError business
rejection. Usage:
    uv run --project platform/api python platform/api/scripts/mcp_demo.py [--url URL] [--tokens DIR]
Defaults: URL from PLATFORM_MCP_URL or http://localhost:8082/mcp; tokens resolved relative
to this file (deploy/pilot/devauth/tokens) unless PLATFORM_DEVAUTH_OUT_DIR is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).resolve().parent
DEFAULT_TOKENS = HERE.parents[2] / "deploy" / "pilot" / "devauth" / "tokens"


def token(tokens: Path, user: str, aud: str) -> str:
    return (tokens / f"{user}.{aud}.jwt").read_text().strip()


def show(label: str, value: Any) -> None:
    print(f"\n### {label}")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str)[:2500])


async def session(url: str, bearer: str) -> tuple[Client, httpx2.AsyncClient]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {bearer}"}, timeout=30)
    return Client(streamable_http_client(url, http_client=http)), http


async def call(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, args)
    return {"isError": result.is_error, "structuredContent": result.structured_content}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("PLATFORM_MCP_URL", "http://localhost:8082/mcp"))
    parser.add_argument(
        "--tokens",
        type=Path,
        default=Path(os.environ["PLATFORM_DEVAUTH_OUT_DIR"]) / "tokens"
        if os.environ.get("PLATFORM_DEVAUTH_OUT_DIR")
        else DEFAULT_TOKENS,
    )
    parser.add_argument("--aud", default=os.environ.get("PLATFORM_MCP_AUD", "mcp-aud"))
    args = parser.parse_args()
    run = uuid.uuid4().hex[:8]

    viewer, vhttp = await session(args.url, token(args.tokens, "viewer", args.aud))
    async with vhttp, viewer:
        tools = await viewer.list_tools()
        show("tools/list as viewer@bravo.test", [t.name for t in tools.tools])
        try:
            await viewer.call_tool("gate_run", {"plan_id": str(uuid.uuid4()), "idempotency_key": f"gate-{run}"})
        except Exception as exc:  # the server answers HTTP 403 before the SDK runs
            show("viewer tools/call gate_run -> HTTP layer", f"{type(exc).__name__}: {exc}")

    operator, ohttp = await session(args.url, token(args.tokens, "operator", args.aud))
    async with ohttp, operator:
        tools = await operator.list_tools()
        show(
            "tools/list as operator@bravo.test",
            [
                {"name": t.name, "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None}
                for t in tools.tools
            ],
        )
        src = await call(
            operator,
            "env_register",
            {
                "name": f"chat-src-{run}",
                "role": "source",
                "secret_ref": "tenants/t1/chat/src-db",
                "idempotency_key": f"env-src-{run}",
            },
        )
        dst = await call(
            operator,
            "env_register",
            {
                "name": f"chat-dst-{run}",
                "role": "target",
                "secret_ref": "tenants/t1/chat/dst-db",
                "idempotency_key": f"env-dst-{run}",
            },
        )
        show("env_register (source, target)", [src, dst])
        plan = await call(
            operator,
            "plan_create",
            {
                "workload": "chat",
                "source_environment_id": src["structuredContent"]["environment_id"],
                "target_environment_id": dst["structuredContent"]["environment_id"],
                "idempotency_key": f"plan-{run}",
            },
        )
        show("plan_create", plan)
        plan_id = plan["structuredContent"]["plan_id"]
        show("plan_get", await call(operator, "plan_get", {"plan_id": plan_id}))
        gate = await call(operator, "gate_run", {"plan_id": plan_id, "idempotency_key": f"gate-{run}"})
        show("gate_run", gate)
        replay = await call(operator, "gate_run", {"plan_id": plan_id, "idempotency_key": f"gate-{run}"})
        show(
            "gate_run replay (same key) -> same verdict_id",
            {"same": replay["structuredContent"]["verdict_id"] == gate["structuredContent"]["verdict_id"]},
        )
        show("plan_events", await call(operator, "plan_events", {"plan_id": plan_id, "after": 0, "limit": 20}))
        show("business rejection -> isError", await call(operator, "plan_get", {"plan_id": str(uuid.uuid4())}))
        show(
            "same key, different input -> isError",
            await call(
                operator,
                "env_register",
                {
                    "name": f"chat-other-{run}",
                    "role": "source",
                    "secret_ref": "tenants/t1/chat/src-db",
                    "idempotency_key": f"env-src-{run}",
                },
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
