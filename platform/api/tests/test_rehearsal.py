"""S2 rehearsal path through the API against a real postgres:16 and the real platform/rehearsal
service (started from its own virtualenv in a child process with a clean environment).

No model key is configured on this machine, so the service must fail closed with
``llm_unconfigured`` and the plan must record it without counting an attempt (FR-5). Nothing
here produces a PASS or FAIL verdict: those come only from the service's verifier.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode, Settings
from bravo_api.db import Database
from bravo_api.rehearsal import _row, artifact_digest, bound_problem, settle

from .conftest import ExecutorStub, Keys, free_port, make_settings
from .test_api_flow import Console

REHEARSAL_DIR = Path(__file__).resolve().parents[2] / "rehearsal"
REHEARSAL_PY = REHEARSAL_DIR / ".venv" / "bin" / "python"
SERVICE_TOKEN = "svc-" + uuid.uuid4().hex


@pytest.fixture(scope="module")
def rehearsal_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not REHEARSAL_PY.is_file():
        pytest.skip(f"rehearsal_venv_missing: {REHEARSAL_PY.relative_to(REHEARSAL_DIR.parent)}")
    port = free_port()
    env = {  # built from scratch: no model key, no kubeconfig, nothing inherited
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path_factory.mktemp("rehearsal-home")),
        "BRAVO_REHEARSAL_API_TOKEN": SERVICE_TOKEN,
        "BRAVO_REHEARSAL_TOKEN_KEY": uuid.uuid4().hex + uuid.uuid4().hex,
        "BRAVO_REHEARSAL_TOOL_LAYER_URL": f"http://127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [
            str(REHEARSAL_PY),
            "-m",
            "uvicorn",
            "bravo_rehearsal.service:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=REHEARSAL_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline or proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout and proc.poll() is not None else ""
                raise RuntimeError(f"rehearsal service did not start: {out[-2000:]}")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(scope="module")
def api_settings(keys: Keys, pg: dict[str, str], executor: ExecutorStub, rehearsal_service: str) -> Settings:
    return replace(
        make_settings(keys, pg, executor.url, Mode.API), rehearsal_url=rehearsal_service, rehearsal_poll_interval_s=0.2
    )


@pytest.fixture(scope="module")
def api(api_settings: Settings) -> Iterator[TestClient]:
    os.environ["BRAVO_REHEARSAL_API_TOKEN"] = SERVICE_TOKEN
    try:
        with TestClient(create_app(api_settings)) as client:
            yield client
    finally:
        os.environ.pop("BRAVO_REHEARSAL_API_TOKEN", None)


@pytest.fixture()
def console(api: TestClient, keys: Keys, executor: ExecutorStub) -> Console:
    executor.healthy()
    return Console(api, keys)


def test_service_itself_reports_llm_unconfigured(rehearsal_service: str) -> None:
    body = {
        "plan_id": str(uuid.uuid4()),
        "tenant_id": "t1",
        "rehearsal_id": "r",
        "attempt": 1,
        "max_attempts": 3,
        "task": "fix-bucket-policy",
        "role": "governance",
    }
    r = httpx.post(
        f"{rehearsal_service}/v1/trials",
        json=body,
        headers={"Idempotency-Key": "direct-1", "Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert r.status_code == 503 and r.json()["error"] == "llm_unconfigured"


def test_gate_block_to_rehearsal_fails_closed_llm_unconfigured(
    console: Console, executor: ExecutorStub, pg: dict[str, str]
) -> None:
    executor.controls[0]["dst"] = "FAIL"  # S1: target attachments readable without login
    pid = console.plan()
    assert console.get("operator", f"/plans/{pid}").json()["state"] == "GATE"
    assert console.post("operator", f"/plans/{pid}/gate").json()["verdict"] == "BLOCK"
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "INFRA_ERROR" and out["reason"] == "llm_unconfigured" and out["state"] == "PROPOSE"
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "PROPOSE" and plan["state_reason"] == "llm_unconfigured"
    assert plan["rehearsal"] == "infra_error"
    assert plan["rehearsal_attempts"] == {"failed": 0, "infra_errors": 0, "max": 3}  # precondition: not counted
    assert plan["last_rehearsal"]["status"] == "INFRA_ERROR" and plan["last_rehearsal"]["trial_id"] is None
    with psycopg.connect(pg["owner"]) as c:
        rows = c.execute("SELECT kind, data FROM events WHERE plan_id = %s ORDER BY seq", (pid,)).fetchall()
    kinds = [k for k, _ in rows]
    assert kinds[-4:] == ["state.changed", "trial.started", "trial.verdict", "state.changed"]
    assert rows[-2][1]["reason"] == "llm_unconfigured" and rows[-2][1]["verdict"] == "INFRA_ERROR"
    # The gate cannot be passed from PROPOSE: a proposal must go through the rehearsal first.
    executor.healthy()
    assert console.post("operator", f"/plans/{pid}/gate").json()["error"] == "state_mismatch"


def test_role_must_belong_to_the_stage(console: Console) -> None:
    pid = console.plan()
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "discovery"})
    assert r.status_code == 422


def test_rehearsal_refused_after_cutover(console: Console, pg: dict[str, str]) -> None:
    pid = console.plan()
    with psycopg.connect(pg["owner"], autocommit=True) as c:  # a plan whose cutover is committed
        c.execute("UPDATE plans SET state = 'WATCH' WHERE plan_id = %s", (pid,))
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
    assert r.status_code == 409 and r.json()["error"] == "rehearsal_not_allowed"  # S2-AC5


def test_verdict_binding_rejects_unbound_results() -> None:
    row: dict[str, Any] = {"trial_id": "t-1", "plan_id": uuid.UUID(int=1), "attempt": 2}
    artifact = {"a": 1}
    good_meta = {
        "trial_id": "t-1",
        "plan_id": str(uuid.UUID(int=1)),
        "attempt": 2,
        "verdict": "FAIL",
        "artifact": artifact,
        "artifact_digest": artifact_digest(artifact),
    }
    assert bound_problem({**good_meta, "attempt": 1}, row) == "verdict_unbound:attempt"  # previous attempt (V5)
    assert bound_problem({**good_meta, "trial_id": "t-2"}, row) == "verdict_unbound:trial"  # other Trial (V5)
    assert bound_problem({**good_meta, "artifact": {"a": 2}}, row) == "verdict_unbound:artifact"  # tampered (V5)
    assert bound_problem(good_meta, row) == "verdict_unbound:verifier"  # no verifier record -> not accepted


def test_infra_error_cap_sends_plan_to_needs_human(console: Console, pg: dict[str, str], api_settings: Settings) -> None:
    """The real failure mode: the rehearsal service restarted and no longer knows the Trial (404)."""
    pid = console.plan()
    with psycopg.connect(pg["owner"], autocommit=True) as c:
        c.execute("UPDATE plans SET state = 'PROPOSE' WHERE plan_id = %s", (pid,))
        row0 = c.execute("SELECT id FROM principals WHERE email = 'operator@bravo.test'").fetchone()
        assert row0 is not None
        principal = row0[0]

    async def run() -> list[str]:
        db = Database(pg["bravo_api"], 1, 2)
        await db.open()
        states = []
        try:
            for n in range(1, 4):
                rid = uuid.uuid4()
                async with db.tenant_tx("t1", principal) as conn:
                    await conn.execute(
                        """INSERT INTO rehearsals (rehearsal_id, tenant_id, plan_id, attempt, task, role, idem_key,
                                                   status, trial_id, requested_by, channel)
                           VALUES (%s, 't1', %s, %s, 'fix-bucket-policy', 'governance', %s, 'running', %s, %s,
                                   'console')""",
                        (rid, pid, n, f"rehearsal-{rid}", f"t-lost-{n}", principal),
                    )
                    await conn.execute("UPDATE plans SET active_rehearsal_id = %s WHERE plan_id = %s", (rid, pid))
                    row = await _row(conn, rid)
                    plan = await settle(conn, api_settings, row, {"verdict": "INFRA_ERROR", "reason": "trial_lost"})
                    states.append(plan["state"])
        finally:
            await db.close()
        return states

    assert asyncio.run(run()) == ["PROPOSE", "PROPOSE", "NEEDS_HUMAN"]  # cap 2 (07 3)
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state_reason"] == "rehearsal_infra_errors_exhausted" and plan["rehearsal"] == "needs_human"
    assert plan["rehearsal_attempts"]["infra_errors"] == 3 and plan["rehearsal_attempts"]["failed"] == 0


def test_rehearsal_start_over_operator_mcp_uses_mcp_role(
    api_settings: Settings, console: Console, keys: Keys, executor: ExecutorStub, pg: dict[str, str]
) -> None:
    """Same handler through operator-MCP (DB role ``bravo_mcp``): grants, RLS and idempotency policy."""
    from .conftest import MCP_AUD, email
    from .test_mcp import call, rpc

    executor.controls[0]["dst"] = "FAIL"
    pid = console.plan()
    console.post("operator", f"/plans/{pid}/gate")
    mcp_settings = replace(api_settings, mode=Mode.MCP, database_url=pg["bravo_mcp"])
    os.environ["BRAVO_REHEARSAL_API_TOKEN"] = SERVICE_TOKEN
    try:
        with TestClient(create_app(mcp_settings)) as mcp:
            op = keys.token(email("operator"), MCP_AUD)
            args = {"plan_id": pid, "task": "fix-bucket-policy", "role": "governance", "idempotency_key": f"m-r-{pid}"}
            out = call(mcp, op, "rehearsal_start", args)["result"]
            assert out["isError"] is False
            sc = out["structuredContent"]
            assert sc["status"] == "INFRA_ERROR" and sc["reason"] == "llm_unconfigured" and sc["state"] == "PROPOSE"
            again = call(mcp, op, "rehearsal_start", args)["result"]["structuredContent"]
            assert again == sc  # idempotent replay
            viewer = keys.token(email("viewer"), MCP_AUD)
            denied = rpc(mcp, viewer, "tools/call", {"name": "rehearsal_start", "arguments": args})
            assert denied.status_code == 403 and denied.json()["error"] == "insufficient_scope"
    finally:
        os.environ.pop("BRAVO_REHEARSAL_API_TOKEN", None)
