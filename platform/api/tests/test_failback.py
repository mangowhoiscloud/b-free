"""WATCH -> DONE (control re-test) and WATCH -> FAILBACK -> DONE_ROLLED_BACK (S4) through the
console API, a real postgres:16 and the Executor HTTP contract (``POST /v1/failback``)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode

from .conftest import ExecutorStub, Keys, make_settings
from .test_api_flow import Console, wait_state


@pytest.fixture(scope="module")
def api(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(keys, pg, executor.url, Mode.API))) as client:
        yield client


@pytest.fixture()
def console(api: TestClient, keys: Keys, executor: ExecutorStub) -> Console:
    executor.healthy()
    return Console(api, keys)


def executor_finishes(pg: dict[str, str], run_id: str, pid: str, kind: str, epoch: int) -> None:
    """What platform/executor writes with its own role when a run ends (CONTRACT.md 5)."""
    with psycopg.connect(pg["bravo_executor"], autocommit=True) as ex:
        ex.execute(
            "INSERT INTO runs (run_id, plan_id, kind, epoch, idem_key, status) VALUES (%s, %s, %s, %s, %s, 'RUNNING')",
            (run_id, pid, kind, epoch, f"{kind}-{uuid.uuid4()}"),
        )
        ex.execute(
            "INSERT INTO receipts (receipt_id, run_id, body, verdict) VALUES (%s, %s, %s, 'MATCH')",
            (uuid.uuid4(), run_id, json.dumps({"verdict": "MATCH", "direction": kind, "epoch": epoch})),
        )
        ex.execute("UPDATE runs SET status = 'DONE', write_pause_ms = 300, ended_at = now() WHERE run_id = %s", (run_id,))


def to_watch(console: Console, pg: dict[str, str]) -> tuple[str, str]:
    pid = console.plan()
    gate = console.post("operator", f"/plans/{pid}/gate").json()
    assert gate["state"] == "AWAIT_APPROVAL"
    decision = {"approval_request_id": gate["approval_request_id"], "revision": 1, "gate_verdict_id": gate["verdict_id"]}
    assert console.post("approver", f"/plans/{pid}/approvals", decision).status_code == 201
    run_id = console.post("operator", f"/plans/{pid}/shift", {"stage": "cutover"}).json()["run_id"]
    executor_finishes(pg, run_id, pid, "cutover", 1)
    wait_state(console, pid, "WATCH")
    return pid, run_id


def test_control_retest_in_watch_closes_the_move(console: Console, pg: dict[str, str]) -> None:
    pid, _ = to_watch(console, pg)
    r = console.post("operator", f"/plans/{pid}/gate").json()
    assert r["verdict"] == "PASS" and r["state"] == "DONE"
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "DONE" and plan["awaiting_approval"] is None


def test_control_regression_in_watch_keeps_watching(console: Console, executor: ExecutorStub, pg: dict[str, str]) -> None:
    pid, _ = to_watch(console, pg)
    executor.readiness["lag_bytes"] = 10**9  # replication readiness is not part of the WATCH re-test
    executor.controls[1]["dst"] = "FAIL"
    r = console.post("operator", f"/plans/{pid}/gate").json()
    assert r["verdict"] == "BLOCK" and r["state"] == "WATCH" and r["reasons"] == ["DB-AUTH:regression"]
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state_reason"] == "controls_regressed" and plan["awaiting_approval"]["stage"] == "failback"


def test_failback_flow(console: Console, executor: ExecutorStub, pg: dict[str, str]) -> None:
    pid, cutover_run = to_watch(console, pg)
    plan = console.get("viewer", f"/plans/{pid}").json()
    awaiting: dict[str, Any] = plan["awaiting_approval"]
    assert awaiting["stage"] == "failback" and awaiting["bound_run_id"] == cutover_run
    # No failback without its own approval; a cutover-stage decision does not fit.
    assert console.post("operator", f"/plans/{pid}/shift", {"stage": "failback"}).json()["error"] == "approval_missing"
    wrong = {"stage": "cutover", "approval_request_id": awaiting["approval_request_id"], "revision": awaiting["revision"]}
    assert console.post("approver", f"/plans/{pid}/approvals", wrong).json()["error"] == "state_mismatch"
    decision = {
        "stage": "failback",
        "approval_request_id": awaiting["approval_request_id"],
        "revision": awaiting["revision"],
    }
    assert console.post("operator", f"/plans/{pid}/approvals", decision).status_code == 403
    granted = console.post("approver", f"/plans/{pid}/approvals", decision)
    assert granted.status_code == 201, granted.text
    assert granted.json()["stage"] == "failback" and granted.json()["bound_run_id"] == cutover_run
    started = console.post("operator", f"/plans/{pid}/shift", {"stage": "failback"}, key="failback-key-0001")
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["state"] == "FAILBACK" and body["epoch"] == 2 and body["run_id"]
    assert executor.failback_bodies[-1]["epoch"] == 2
    assert executor.failback_bodies[-1]["approval_id"] == granted.json()["approval_id"]
    executor_finishes(pg, body["run_id"], pid, "failback", 2)
    done = wait_state(console, pid, "DONE_ROLLED_BACK")
    assert done["run"]["kind"] == "failback" and done["last_receipt"]["verdict"] == "MATCH"
    # S2-AC5: no rehearsal on a plan whose move was committed.
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
    assert r.status_code == 409 and r.json()["error"] == "rehearsal_not_allowed"
