"""Operator MCP over Streamable HTTP (stateless, JSON responses): role-filtered tools/list,
HTTP 401/403 for identity and role, isError for business rejections, JSON-RPC errors for
malformed arguments. Approval, shift and rehearsal tools do not exist."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode

from .conftest import CONSOLE_AUD, MCP_AUD, ExecutorStub, Keys, email, make_settings

_ids = itertools.count(1)
READ_TOOLS = {"env_controls", "plan_get", "plan_events"}
ALL_TOOLS = READ_TOOLS | {"env_register", "plan_create", "gate_run", "rehearsal_start"}


@pytest.fixture(scope="module")
def mcp(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(keys, pg, executor.url, Mode.MCP))) as client:
        yield client


def rpc(
    client: TestClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        **(headers or {}),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=h)


def call(client: TestClient, token: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    r = rpc(client, token, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize(("user", "expected"), [("viewer", READ_TOOLS), ("approver", READ_TOOLS), ("operator", ALL_TOOLS)])
def test_tools_list_by_role(mcp: TestClient, keys: Keys, user: str, expected: set[str]) -> None:
    r = rpc(mcp, keys.token(email(user), MCP_AUD), "tools/list")
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == expected
    for name in tools:
        t = tools[name]
        assert "outputSchema" in t and t["annotations"]["openWorldHint"] is False
        assert t["annotations"]["readOnlyHint"] is (name in READ_TOOLS)
        assert t["annotations"].get("destructiveHint") in (None, False)


def test_no_approval_or_shift_tool_exists(mcp: TestClient, keys: Keys) -> None:
    r = rpc(mcp, keys.token(email("operator"), MCP_AUD), "tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert len(names) == 7
    assert not {n for n in names if any(w in n for w in ("approv", "shift", "cutover", "failback", "weight"))}


@pytest.mark.parametrize(
    ("user", "tool"),
    [("viewer", "gate_run"), ("viewer", "plan_create"), ("approver", "env_register"), ("approver", "gate_run")],
)
def test_per_call_role_check_is_http_403(mcp: TestClient, keys: Keys, user: str, tool: str) -> None:
    r = rpc(
        mcp,
        keys.token(email(user), MCP_AUD),
        "tools/call",
        {"name": tool, "arguments": {"plan_id": str(uuid.uuid4()), "idempotency_key": "k-12345678"}},
    )
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]


def test_identity_failures_are_http_401(mcp: TestClient, keys: Keys) -> None:
    assert rpc(mcp, None, "tools/list").status_code == 401
    console_token = keys.token(email("operator"), CONSOLE_AUD)
    r = rpc(mcp, console_token, "tools/list")
    assert r.status_code == 401 and r.json()["error"] == "wrong_audience"
    r = rpc(mcp, None, "tools/list", headers={"Cf-Access-Jwt-Assertion": keys.token(email("operator"), MCP_AUD)})
    assert r.status_code == 401


def test_origin_not_allowed(mcp: TestClient, keys: Keys) -> None:
    r = rpc(mcp, keys.token(email("viewer"), MCP_AUD), "tools/list", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "origin_not_allowed"


def test_business_rejection_is_is_error(mcp: TestClient, keys: Keys) -> None:
    out = call(mcp, keys.token(email("viewer"), MCP_AUD), "plan_get", {"plan_id": str(uuid.uuid4())})
    assert out["result"]["isError"] is True
    assert out["result"]["structuredContent"] == {"error": "plan_not_found"}


def test_malformed_arguments_are_jsonrpc_error(mcp: TestClient, keys: Keys) -> None:
    out = call(mcp, keys.token(email("operator"), MCP_AUD), "gate_run", {"plan_id": "not-a-uuid"})
    assert "error" in out and out["error"]["code"] == -32602


def test_operator_flow_over_mcp(mcp: TestClient, keys: Keys, executor: ExecutorStub) -> None:
    executor.healthy()
    executor.controls[0]["dst"] = "FAIL"
    op = keys.token(email("operator"), MCP_AUD)
    tag = uuid.uuid4().hex[:8]
    src = call(
        mcp,
        op,
        "env_register",
        {"name": f"m-src-{tag}", "role": "source", "secret_ref": "tenants/t1/s", "idempotency_key": f"m-src-{tag}"},
    )
    dst = call(
        mcp,
        op,
        "env_register",
        {"name": f"m-dst-{tag}", "role": "target", "secret_ref": "tenants/t1/d", "idempotency_key": f"m-dst-{tag}"},
    )
    s, d = src["result"]["structuredContent"], dst["result"]["structuredContent"]
    plan = call(
        mcp,
        op,
        "plan_create",
        {
            "workload": "chat",
            "source_environment_id": s["environment_id"],
            "target_environment_id": d["environment_id"],
            "idempotency_key": f"m-plan-{tag}",
        },
    )["result"]["structuredContent"]
    assert plan["state"] == "GATE" and plan["rehearsal"] == "not_started"
    gate = call(mcp, op, "gate_run", {"plan_id": plan["plan_id"], "idempotency_key": f"m-gate-{tag}"})
    g = gate["result"]["structuredContent"]
    assert g["verdict"] == "BLOCK" and g["reasons"] == ["DB-ANON-READ:regression"] and g["state"] == "GATE"
    assert gate["result"]["content"][0]["type"] == "text"  # same JSON as text, per spec advice
    replay = call(mcp, op, "gate_run", {"plan_id": plan["plan_id"], "idempotency_key": f"m-gate-{tag}"})
    assert replay["result"]["structuredContent"]["verdict_id"] == g["verdict_id"]
    executor.healthy()
    passed = call(mcp, op, "gate_run", {"plan_id": plan["plan_id"], "idempotency_key": f"m-gate2-{tag}"})
    p = passed["result"]["structuredContent"]
    assert p["verdict"] == "PASS" and p["state"] == "AWAIT_APPROVAL"
    view = call(mcp, keys.token(email("viewer"), MCP_AUD), "plan_get", {"plan_id": plan["plan_id"]})
    assert view["result"]["structuredContent"]["awaiting_approval"]["approve_url"].startswith("http://console.test/")
    events = call(mcp, op, "plan_events", {"plan_id": plan["plan_id"], "after": 0, "limit": 50})
    evs = events["result"]["structuredContent"]["events"]
    assert [e["kind"] for e in evs] == [
        "state.changed",  # created -> COLLECT
        "state.changed",  # COLLECT -> ANALYZE
        "analysis.done",
        "state.changed",  # ANALYZE -> GATE (the real collector found no code-change / unclassified dependency)
        "gate.verdict",
        "gate.verdict",
        "state.changed",  # GATE -> AWAIT_APPROVAL
        "approval.requested",  # stage cutover, bound to the revision and the gate verdict
    ]
    assert evs[2]["data"]["inventory"]["status"] == "ok" and evs[2]["data"]["gaps"] == []
    assert evs[-1]["data"]["stage"] == "cutover" and evs[-1]["data"]["gate_verdict_id"] == p["verdict_id"]
    assert {e["mode"] for e in evs} == {"live"} and {e["actor"]["kind"] for e in evs} == {"mcp"}
    assert [int(e["id"]) for e in evs] == sorted(int(e["id"]) for e in evs)
    reuse = call(
        mcp,
        op,
        "env_register",
        {"name": "other", "role": "source", "secret_ref": "tenants/t1/s", "idempotency_key": f"m-src-{tag}"},
    )
    assert reuse["result"]["isError"] is True
    assert reuse["result"]["structuredContent"]["error"] == "idempotency_key_reuse"
