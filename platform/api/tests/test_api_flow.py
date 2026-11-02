"""End-to-end console flow against a real postgres:16 (RLS, triggers, grants) and an
Executor HTTP stub: COLLECT -> GATE BLOCK -> fix -> PASS -> approval -> shift -> WATCH."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode

from .conftest import CONSOLE_AUD, MCP_AUD, ExecutorStub, Keys, email, make_settings


@pytest.fixture(scope="module")
def api(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(keys, pg, executor.url, Mode.API))) as client:
        yield client


class Console:
    def __init__(self, client: TestClient, keys: Keys) -> None:
        self.c, self.keys = client, keys

    def h(self, user: str, key: str | None = None, aud: str = CONSOLE_AUD) -> dict[str, str]:
        headers = {"Cf-Access-Jwt-Assertion": self.keys.token(email(user), aud)}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def post(self, user: str, path: str, body: dict[str, Any] | None = None, key: str | None = None) -> Any:
        return self.c.post(f"/v1{path}", json=body, headers=self.h(user, key or f"k-{uuid.uuid4().hex}"))

    def get(self, user: str, path: str) -> Any:
        return self.c.get(f"/v1{path}", headers=self.h(user))

    def plan(self, user: str = "operator") -> str:
        tag = uuid.uuid4().hex[:8]
        src = self.post(
            user, "/environments", {"name": f"src-{tag}", "role": "source", "secret_ref": "tenants/t1/src"}
        ).json()
        dst = self.post(
            user, "/environments", {"name": f"dst-{tag}", "role": "target", "secret_ref": "tenants/t1/dst"}
        ).json()
        r = self.post(
            user,
            "/plans",
            {
                "workload": "chat",
                "source_environment_id": src["environment_id"],
                "target_environment_id": dst["environment_id"],
            },
        )
        assert r.status_code == 201, r.text
        return r.json()["plan_id"]


@pytest.fixture()
def console(api: TestClient, keys: Keys, executor: ExecutorStub) -> Console:
    executor.healthy()
    return Console(api, keys)


def test_auth_layers(console: Console, keys: Keys) -> None:
    pid = str(uuid.uuid4())
    assert console.c.get(f"/v1/plans/{pid}").status_code == 401
    r = console.c.get(f"/v1/plans/{pid}", headers={"Authorization": f"Bearer {keys.token(email('operator'), MCP_AUD)}"})
    assert r.status_code == 401 and r.json()["error"] == "wrong_audience"
    two = {
        "Cf-Access-Jwt-Assertion": keys.token(email("operator"), CONSOLE_AUD),
        "Authorization": f"Bearer {keys.token(email('operator'), CONSOLE_AUD)}",
    }
    assert console.c.get(f"/v1/plans/{pid}", headers=two).status_code == 401
    stranger = {"Cf-Access-Jwt-Assertion": keys.token("stranger@bravo.test", CONSOLE_AUD)}
    r = console.c.get(f"/v1/plans/{pid}", headers=stranger)
    assert r.status_code == 403 and r.json()["error"] == "unknown_principal"
    assert console.get("viewer", f"/plans/{pid}").status_code == 404


def test_idempotency_replay_and_conflict(console: Console) -> None:
    body = {"name": f"env-{uuid.uuid4().hex[:8]}", "role": "source", "secret_ref": "tenants/t1/a"}
    first = console.post("operator", "/environments", body, key="idem-test-0001")
    again = console.post("operator", "/environments", body, key="idem-test-0001")
    assert first.status_code == 201 and again.json()["environment_id"] == first.json()["environment_id"]
    other = console.post("operator", "/environments", {**body, "name": "different"}, key="idem-test-0001")
    assert other.status_code == 409 and other.json()["error"] == "idempotency_key_reuse"
    missing = console.c.post("/v1/environments", json=body, headers=console.h("operator"))
    assert missing.status_code == 400 and missing.json()["error"] == "idempotency_key_required"


def test_secret_ref_must_stay_in_tenant(console: Console) -> None:
    r = console.post("operator", "/environments", {"name": "x1", "role": "source", "secret_ref": "tenants/t2/db"})
    assert r.status_code == 422 and r.json()["error"] == "secret_ref_out_of_scope"


def wait_state(console: Console, pid: str, state: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        plan = console.get("viewer", f"/plans/{pid}").json()
        if plan["state"] == state:
            return plan
        time.sleep(0.1)
    raise AssertionError(f"plan stayed in {plan['state']}")


def test_full_pilot_flow(console: Console, executor: ExecutorStub, pg: dict[str, str]) -> None:
    executor.controls[0]["dst"] = "FAIL"  # planted regression: PUBLIC can read messages on dst
    pid = console.plan()
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "GATE" and plan["rehearsal"] == "not_started" and plan["collected_at"]

    block = console.post("operator", f"/plans/{pid}/gate").json()
    assert block["verdict"] == "BLOCK" and block["reasons"] == ["DB-ANON-READ:regression"]
    assert block["state"] == "GATE" and block["approval_request_id"] is None

    executor.healthy()  # a human ran fix-dst-grant.sql
    gate = console.post("operator", f"/plans/{pid}/gate").json()
    assert gate["verdict"] == "PASS" and gate["state"] == "AWAIT_APPROVAL"
    viewed = console.get("viewer", f"/plans/{pid}").json()
    awaiting = viewed["awaiting_approval"]
    assert awaiting["approval_request_id"] == gate["approval_request_id"]
    assert awaiting["expires_at"] and awaiting["gate_verdict_id"] == gate["verdict_id"] and awaiting["revision"] == 1
    assert viewed["created_by"] == {"principal": "operator@bravo.test"}
    assert viewed["last_gate"]["readiness"]["schema_hash"]["src"] == "abc"
    assert gate["readiness"]["seq"] == {"src": 10, "dst": 10} and gate["readiness"]["schema_match"] is True

    decision = {
        "approval_request_id": awaiting["approval_request_id"],
        "revision": awaiting["revision"],
        "gate_verdict_id": gate["verdict_id"],
        "approver": "operator@bravo.test",
        "approver_id": str(uuid.uuid4()),
    }
    assert console.post("operator", f"/plans/{pid}/approvals", decision).status_code == 403
    assert console.post("viewer", f"/plans/{pid}/approvals", decision).status_code == 403
    granted = console.post("approver", f"/plans/{pid}/approvals", decision)
    assert granted.status_code == 201, granted.text
    assert granted.json()["approver"] == "approver@bravo.test"  # body "approver" ignored (A3)
    assert granted.json()["expires_at"] == awaiting["expires_at"]  # the approval binds the shown expiry
    reused = console.post("approver", f"/plans/{pid}/approvals", decision)
    assert reused.status_code == 409 and reused.json()["error"] == "approval_request_used"

    assert console.post("approver", f"/plans/{pid}/shift", {"stage": "cutover"}).status_code == 403
    shift = console.post("operator", f"/plans/{pid}/shift", {"stage": "cutover"}, key="shift-key-0001")
    assert shift.status_code == 202, shift.text
    run_id = shift.json()["run_id"]
    assert run_id and executor.cutover_bodies[-1]["epoch"] == 1
    assert executor.cutover_bodies[-1]["approval_id"] == granted.json()["approval_id"]
    replay = console.post("operator", f"/plans/{pid}/shift", {"stage": "cutover"}, key="shift-key-0001")
    assert replay.json()["run_id"] == run_id  # A8
    second = console.post("operator", f"/plans/{pid}/shift", {"stage": "cutover"})
    assert second.status_code == 409  # approval consumed / state moved on

    # The Executor writes the contract tables with its own role.
    with psycopg.connect(pg["bravo_executor"], autocommit=True) as ex:
        ex.execute(
            "INSERT INTO runs (run_id, plan_id, kind, epoch, idem_key, status) VALUES (%s, %s, 'cutover', 1, %s, 'RUNNING')",
            (run_id, pid, f"cutover-{uuid.uuid4()}"),
        )
        for seq, name in enumerate(["lag_check", "pause", "revoke", "barrier"], start=1):
            ex.execute(
                "INSERT INTO run_stages (run_id, seq, name, started_at, ms, result) VALUES (%s, %s, %s, now(), %s, 'ok')",
                (run_id, seq, name, seq * 10),
            )
        body = {  # shape the Executor writes (deploy/pilot e2e, 2026-11-02)
            "verdict": "MATCH",
            "hash_rule": "v1",
            "epoch": 1,
            "direction": "cutover",
            "source": "src",
            "target": "dst",
            "tables": [
                {
                    "name": "messages",
                    "rows": {"src": 801, "dst": 801},
                    "pk_hash": {"src": "a1", "dst": "a1"},
                    "content_hash": {"src": "c1", "dst": "c1"},
                    "match": True,
                },
                {
                    "name": "bravo_barrier",
                    "rows": {"src": 1, "dst": 1},
                    "pk_hash": {"src": "b1", "dst": "b1"},
                    "content_hash": {"src": "b2", "dst": "b2"},
                    "match": True,
                },
            ],
            "sequences": [
                {"name": "messages_id_seq", "src_last": 801, "dst_set": 801, "dst_before": 0, "match": True},
                {"name": "bravo_barrier_id_seq", "src_last": 1, "dst_set": 1, "dst_before": 0, "match": True},
            ],
            "barrier": {"id": 1, "arrival_ms": 0},
            "objects": [],
        }
        ex.execute(
            "INSERT INTO receipts (receipt_id, run_id, body, verdict) VALUES (%s, %s, %s, 'MATCH')",
            (uuid.uuid4(), run_id, json.dumps(body)),
        )
        ex.execute("UPDATE runs SET status = 'DONE', write_pause_ms = 412, ended_at = now() WHERE run_id = %s", (run_id,))
    plan = wait_state(console, pid, "WATCH")
    assert plan["run"]["status"] == "DONE" and plan["run"]["write_pause_ms"] == 412
    assert plan["last_receipt"]["verdict"] == "MATCH"
    receipt = plan["last_receipt"]["body"]
    assert receipt["tables"] == body["tables"] and receipt["sequences"] == body["sequences"]  # full pass-through
    assert receipt["barrier"] == body["barrier"] and receipt["direction"] == "cutover" and receipt["hash_rule"] == "v1"
    # Console summary: copied verbatim from the single application table / sequence, never combined.
    assert receipt["rows"] == {"src": 801, "dst": 801} and receipt["pk_hash"] == {"src": "a1", "dst": "a1"}
    assert receipt["content_hash"] == {"src": "c1", "dst": "c1"} and receipt["seq"] == {"src": 801, "dst": 801}
    assert receipt["summary_of"] == {"table": "messages", "sequence": "messages_id_seq"}
    last_run = plan["last_run"]
    assert last_run == plan["run"] and last_run["run_id"] == run_id and last_run["status"] == "DONE"
    assert last_run["write_pause_ms"] == 412 and last_run["receipt"]["receipt_id"] == plan["last_receipt"]["receipt_id"]
    assert [(s["seq"], s["name"], s["ms"]) for s in last_run["stages"]] == [
        (1, "lag_check", 10),
        (2, "pause", 20),
        (3, "revoke", 30),
        (4, "barrier", 40),
    ]

    with psycopg.connect(pg["owner"]) as c:
        kinds = [r[0] for r in c.execute("SELECT kind FROM events WHERE plan_id = %s ORDER BY seq", (pid,))]
        modes = {r[0] for r in c.execute("SELECT DISTINCT mode FROM events WHERE plan_id = %s", (pid,))}
    assert modes == {"live"}
    # CUTOVER -> WATCH issues the failback request (approval.requested, stage failback) in the same step.
    assert kinds.count("run.stage") == 4
    assert kinds[-4:] == ["run.done", "receipt.saved", "state.changed", "approval.requested"]
    assert "approval.granted" in kinds and kinds.count("gate.verdict") == 2
    with psycopg.connect(pg["owner"]) as c:
        saved = c.execute("SELECT data FROM events WHERE plan_id = %s AND kind = 'receipt.saved'", (pid,)).fetchone()
        verdict_ev = c.execute(
            "SELECT data FROM events WHERE plan_id = %s AND kind = 'gate.verdict' ORDER BY seq DESC", (pid,)
        ).fetchone()
    assert saved and saved[0]["body"]["tables"] == body["tables"] and saved[0]["body"]["sequences"] == body["sequences"]
    assert saved[0]["body"]["content_hash"] == {"src": "c1", "dst": "c1"}
    assert verdict_ev and verdict_ev[0]["readiness"]["lag_bytes"] == 0
    assert verdict_ev[0]["readiness"]["seq"] == {"src": 10, "dst": 10}
    assert {c["control_id"] for c in verdict_ev[0]["controls"]} == {"DB-ANON-READ", "DB-AUTH"}


def test_separation_of_duties_enforced_by_db(console: Console) -> None:
    pid = console.plan(user="dual")  # dual holds operator AND approver
    gate = console.post("dual", f"/plans/{pid}/gate").json()
    assert gate["state"] == "AWAIT_APPROVAL"
    r = console.post("dual", f"/plans/{pid}/approvals", {"approval_request_id": gate["approval_request_id"], "revision": 1})
    assert r.status_code == 403 and r.json()["error"] == "separation_of_duties"


def test_new_gate_voids_old_approval(console: Console) -> None:
    pid = console.plan()
    gate = console.post("operator", f"/plans/{pid}/gate").json()
    ok = console.post(
        "approver", f"/plans/{pid}/approvals", {"approval_request_id": gate["approval_request_id"], "revision": 1}
    )
    assert ok.status_code == 201
    regate = console.post("operator", f"/plans/{pid}/gate").json()
    assert regate["approval_request_id"] != gate["approval_request_id"]
    r = console.post("operator", f"/plans/{pid}/shift", {"stage": "cutover"})
    assert r.status_code == 409 and r.json()["error"] == "approval_missing"
    stale = console.post(
        "approver", f"/plans/{pid}/approvals", {"approval_request_id": gate["approval_request_id"], "revision": 1}
    )
    assert stale.status_code == 409 and stale.json()["error"] == "approval_stale"


def test_executor_down_blocks_deterministically(console: Console, keys: Keys, pg: dict[str, str]) -> None:
    app = create_app(make_settings(keys, pg, "http://127.0.0.1:9", Mode.API))
    with TestClient(app) as client:
        down = Console(client, keys)
        pid = down.plan()
        assert down.get("viewer", f"/plans/{pid}").json()["state_reason"] == "executor_unreachable"
        a = down.post("operator", f"/plans/{pid}/gate").json()
        b = down.post("operator", f"/plans/{pid}/gate").json()
        assert a["verdict"] == b["verdict"] == "BLOCK"
        assert a["input_digest"] == b["input_digest"] and "executor_unreachable" in a["reasons"]
        assert a["state"] == "COLLECT"


def test_sse_ticket_is_plan_bound(console: Console) -> None:
    pid = console.plan()
    other = console.plan()
    ticket = console.c.post(f"/v1/plans/{pid}/events/ticket", headers=console.h("viewer")).json()["ticket"]
    r = console.c.get(f"/v1/plans/{other}/events", params={"ticket": ticket})
    assert r.status_code == 401 and r.json()["error"] == "ticket_wrong_plan"


def test_expired_approval_request_is_stale(console: Console, pg: dict[str, str]) -> None:
    pid = console.plan()
    gate = console.post("operator", f"/plans/{pid}/gate").json()
    with psycopg.connect(pg["owner"], autocommit=True) as c:
        c.execute("UPDATE plans SET approval_request_expires_at = now() - interval '1 second' WHERE plan_id = %s", (pid,))
    r = console.post(
        "approver", f"/plans/{pid}/approvals", {"approval_request_id": gate["approval_request_id"], "revision": 1}
    )
    assert r.status_code == 409
    assert r.json() == {"error": "approval_stale", "detail": {"cause": "approval_request_expired"}}


def test_lost_cutover_response_reads_runs_then_safe_hold(console: Console, executor: ExecutorStub) -> None:
    """Executor answers 5xx (ambiguous): no runs row for the key -> SAFE_HOLD, never a resend."""
    pid = console.plan()
    gate = console.post("operator", f"/plans/{pid}/gate").json()
    console.post("approver", f"/plans/{pid}/approvals", {"approval_request_id": gate["approval_request_id"], "revision": 1})
    sent = len(executor.cutover_bodies)
    executor.fail_cutover = True
    try:
        r = console.post("operator", f"/plans/{pid}/shift", {"stage": "cutover"})
    finally:
        executor.fail_cutover = False
    assert r.status_code == 202 and r.json()["state"] == "SAFE_HOLD"
    assert len(executor.cutover_bodies) == sent + 1  # exactly one attempt
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "SAFE_HOLD" and plan["state_reason"].startswith("cutover_unconfirmed:")
