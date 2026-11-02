"""Spec r2 change approval and the T31 apply hand-off (migration 0004) through the running API.

The plan reaches REHEARSE with SQL (the rehearsal path itself is tested in test_rehearsal.py) and the
rehearsal result is settled with ``rehearsal.settle`` exactly as the watcher does after binding checks.
The T31 worker's side is played with plain SQL as role ``bravo_apply`` (the role platform/apply uses; its
own pipeline is tested in platform/apply). Everything else is the API's own code: its change watcher, its
approval handler, its DB triggers and its system gate re-check against the Executor stub's controls.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from bravo_api.app import create_app
from bravo_api.auth import Channel
from bravo_api.config import Mode, Settings
from bravo_api.db import Database
from bravo_api.errors import ForbiddenError
from bravo_api.models import ApprovalDecision
from bravo_api.rehearsal import _row, artifact_digest, settle
from bravo_api.service import Services

from .conftest import ExecutorStub, Keys, make_settings
from .test_api_flow import Console

DIFF = (
    "--- a/src/bravo_demo/secrets.py\n+++ b/src/bravo_demo/secrets.py\n@@ -1,2 +1,2 @@\n"
    "-SOURCE = 'secretsmanager'\n+SOURCE = 'openbao'\n"
)
ARTIFACT = {
    "role": "app_migration",
    "step": "fix",
    "diff": DIFF,
    "files": ["src/bravo_demo/secrets.py"],
    "rationale": "read the DB password from the platform vault",
    "risk": "low",
}
IMG = "sha256:" + "a9" * 32


@pytest.fixture(scope="module")
def settings(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Settings:
    return make_settings(keys, pg, executor.url, Mode.API)


@pytest.fixture(scope="module")
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture()
def console(api: TestClient, keys: Keys, executor: ExecutorStub) -> Console:
    executor.healthy()
    return Console(api, keys)


def _principal(pg: dict[str, str], email: str) -> uuid.UUID:
    with psycopg.connect(pg["owner"]) as c:
        return c.execute("SELECT id FROM principals WHERE email = %s", (email,)).fetchone()[0]


def _to_rehearse(pg: dict[str, str], pid: str, requester_email: str) -> uuid.UUID:
    rid = uuid.uuid4()
    with psycopg.connect(pg["owner"], autocommit=True) as c, c.transaction():
        c.execute(
            "UPDATE plans SET state = 'REHEARSE', rehearsal = 'running', active_rehearsal_id = %s WHERE plan_id = %s",
            (rid, pid),
        )
        c.execute(
            """INSERT INTO rehearsals (rehearsal_id, tenant_id, plan_id, attempt, task, role, idem_key, status,
                                             trial_id, requested_by, channel)
                     VALUES (%s, 't1', %s, 1, 'fix-secrets-manager', 'app_migration', %s, 'running', 't-test', %s,
                             'console')""",
            (rid, pid, f"rehearsal-{rid}", _principal(pg, requester_email)),
        )
    return rid


def _settle_pass(settings: Settings, pg: dict[str, str], rid: uuid.UUID) -> None:
    async def go() -> None:
        db = Database(pg["bravo_api"])
        await db.open()
        try:
            async with db.tenant_tx("t1") as conn:
                row = await _row(conn, rid)
                await settle(
                    conn,
                    settings,
                    row,
                    {
                        "verdict": "PASS",
                        "trial_id": "t-test",
                        "attempt": 1,
                        "artifact": ARTIFACT,
                        "artifact_digest": artifact_digest(ARTIFACT),
                        "verifier": {
                            "artifact_digest": artifact_digest(ARTIFACT),
                            "run_id": "v1",
                            "pod_uid": "p1",
                            "image_digest": "sha256:" + "0" * 64,
                            "task_version": "1",
                            "collected_at": "now",
                            "reward": 1.0,
                        },
                    },
                )
        finally:
            await db.close()

    asyncio.run(go())


def _worker(pg: dict[str, str], sql: str, params: tuple) -> None:
    with psycopg.connect(pg["bravo_apply"], autocommit=True) as c:
        c.execute(sql, params)


def _stage(pg: dict[str, str], change_id: str, seq: int, phase: str, name: str, result: str = "OK") -> None:
    _worker(
        pg,
        "INSERT INTO change_stages (change_id, seq, phase, name, started_at, ms, result, detail) "
        "VALUES (%s, %s, %s, %s, now(), 5, %s, '{}')",
        (change_id, seq, phase, name, result),
    )


def _wait(fn: Callable[[], Any], what: str, timeout: float = 10.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = fn()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _kinds(pg: dict[str, str], pid: str) -> list[str]:
    with psycopg.connect(pg["owner"]) as c:
        return [r[0] for r in c.execute("SELECT kind FROM events WHERE plan_id = %s ORDER BY seq", (pid,))]


def _prepared_change(
    console: Console, settings: Settings, pg: dict[str, str], requester: str = "operator@bravo.test"
) -> tuple[str, dict[str, Any]]:
    pid = console.plan()
    rid = _to_rehearse(pg, pid, requester)
    _settle_pass(settings, pg, rid)
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "REHEARSE" and plan["rehearsal"] == "passed" and plan["state_reason"] == "change_requested"
    change = plan["change"]
    assert change["status"] == "requested" and change["active"] is True
    cid = change["change_id"]
    _worker(pg, "UPDATE changes SET status = 'preparing', worker = 't' WHERE change_id = %s", (cid,))
    _stage(pg, cid, 1, "prepare", "image.build")
    _worker(
        pg,
        "UPDATE changes SET status = 'prepared', image_repository = 'bravo.local/bravo-demo', image_digest = %s, "
        "sbom_digest = %s, scan = %s, prepared_at = now() WHERE change_id = %s",
        (IMG, "sha256:" + "b0" * 32, '{"scanner": "trivy", "version": "0.76.1", "counts": {"CRITICAL": 0}}', cid),
    )
    awaiting = _wait(lambda: console.get("viewer", f"/plans/{pid}").json()["awaiting_approval"], "change request")
    return pid, awaiting


def test_rehearsal_pass_opens_a_change_and_the_prepared_candidate_gets_a_bound_change_request(
    console: Console, settings: Settings, pg: dict[str, str]
) -> None:
    pid, awaiting = _prepared_change(console, settings, pg)
    assert awaiting["stage"] == "change"
    ch = awaiting["change"]
    import hashlib

    assert ch["diff_digest"] == "sha256:" + hashlib.sha256(DIFF.encode()).hexdigest()
    assert (
        ch["image_digest"] == IMG and ch["policy_diff_digest"] is None and ch["artifact_digest"] == artifact_digest(ARTIFACT)
    )
    assert ch["scan"]["counts"] == {"CRITICAL": 0}
    kinds = _kinds(pg, pid)
    i = kinds.index("change.requested")
    assert kinds[i:] == ["change.requested", "change.stage", "change.prepared", "approval.requested"]
    with psycopg.connect(pg["owner"], row_factory=dict_row) as c:
        ev = c.execute("SELECT data FROM events WHERE plan_id = %s AND kind = 'approval.requested'", (pid,)).fetchone()
    assert (
        ev["data"]["stage"] == "change" and ev["data"]["image_digest"] == IMG and ev["data"]["change_id"] == ch["change_id"]
    )
    detail = console.get("viewer", f"/plans/{pid}/changes/{ch['change_id']}").json()
    assert [(s["seq"], s["phase"], s["name"], s["result"]) for s in detail["stages"]] == [
        (1, "prepare", "image.build", "OK")
    ]
    assert console.get("viewer", f"/plans/{pid}/changes").json()["changes"][0]["change_id"] == ch["change_id"]


def test_change_approval_is_console_only_bound_and_separated(
    console: Console, settings: Settings, pg: dict[str, str], keys: Keys, executor: ExecutorStub
) -> None:
    pid, awaiting = _prepared_change(console, settings, pg, requester="dual@bravo.test")
    cid = awaiting["change"]["change_id"]
    body = {"stage": "change", "approval_request_id": awaiting["approval_request_id"], "revision": awaiting["revision"]}
    # MCP has no approval tool and the handler refuses the MCP channel (D-18, A1).
    mcp_settings = replace(settings, mode=Mode.MCP, database_url=pg["bravo_mcp"])
    from bravo_api.auth import Principal, Role

    async def via_mcp() -> None:
        db = Database(pg["bravo_mcp"])
        svc = Services(db, None, mcp_settings)  # type: ignore[arg-type]
        p = Principal(
            id=_principal(pg, "approver@bravo.test"),
            iss="i",
            sub="s",
            email="approver@bravo.test",
            tenant_id="t1",
            roles=frozenset({Role.APPROVER}),
            channel=Channel.MCP,
        )
        await svc.approve(p, Channel.MCP, uuid.UUID(pid), ApprovalDecision(**body, change_id=cid), "k-mcp-approve-1")

    with pytest.raises(ForbiddenError):
        asyncio.run(via_mcp())
    # Bound to the change shown: missing or other change_id -> stale.
    r = console.post("approver", f"/plans/{pid}/approvals", body)
    assert r.status_code == 409 and r.json()["error"] == "approval_stale"
    r = console.post("approver", f"/plans/{pid}/approvals", {**body, "change_id": str(uuid.uuid4())})
    assert r.status_code == 409 and r.json()["error"] == "approval_stale"
    # The requester of the change cannot approve it (DB trigger), even holding the approver role.
    r = console.post("dual", f"/plans/{pid}/approvals", {**body, "change_id": cid, "approver": "approver@bravo.test"})
    assert r.status_code == 403 and r.json()["error"] == "separation_of_duties"
    r = console.post("approver", f"/plans/{pid}/approvals", {**body, "change_id": cid})
    assert r.status_code == 201, r.text
    a = r.json()
    assert (a["stage"], a["change_id"], a["image_digest"], a["approver"]) == ("change", cid, IMG, "approver@bravo.test")
    assert a["diff_digest"] == awaiting["change"]["diff_digest"]
    with psycopg.connect(pg["owner"], row_factory=dict_row) as c:
        ch = c.execute("SELECT status, approval_id FROM changes WHERE change_id = %s", (cid,)).fetchone()
        ap = c.execute("SELECT * FROM approvals WHERE approval_id = %s", (a["approval_id"],)).fetchone()
    assert ch["status"] == "approved" and str(ch["approval_id"]) == a["approval_id"]
    assert ap["requester_id"] == _principal(pg, "dual@bravo.test") and ap["image_digest"] == IMG
    assert ap["consumed_at"] is not None and ap["run_idem_key"] == f"change-{cid}"
    # Used once.
    r = console.post("approver", f"/plans/{pid}/approvals", {**body, "change_id": cid})
    assert r.status_code == 409


def _approve_change(console: Console, pid: str, awaiting: dict[str, Any]) -> dict[str, Any]:
    r = console.post(
        "approver",
        f"/plans/{pid}/approvals",
        {
            "stage": "change",
            "approval_request_id": awaiting["approval_request_id"],
            "revision": awaiting["revision"],
            "change_id": awaiting["change"]["change_id"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_applied_change_moves_to_gate_and_the_system_recheck_requests_cutover(
    console: Console, settings: Settings, pg: dict[str, str]
) -> None:
    pid, awaiting = _prepared_change(console, settings, pg)
    cid = awaiting["change"]["change_id"]
    _approve_change(console, pid, awaiting)
    _worker(pg, "UPDATE changes SET status = 'applying' WHERE change_id = %s", (cid,))
    for seq, name in ((2, "approval.verify"), (3, "deploy.verify"), (4, "gate.recheck")):
        _stage(pg, cid, seq, "apply", name)
    _worker(
        pg,
        "UPDATE changes SET status = 'applied', deployed_digest = %s, signature = %s, ended_at = now() WHERE change_id = %s",
        (IMG, '{"key_ref": "hashivault://bravo-supply-chain"}', cid),
    )
    plan = _wait(
        lambda: (p := console.get("viewer", f"/plans/{pid}").json())["state"] == "AWAIT_APPROVAL" and p, "gate re-check"
    )
    assert plan["applied_image_digest"] == IMG and plan["change"]["status"] == "applied"
    assert plan["last_gate"]["verdict"] == "PASS" and plan["awaiting_approval"]["stage"] == "cutover"
    assert plan["awaiting_approval"]["applied_image_digest"] == IMG
    kinds = _kinds(pg, pid)
    tail = kinds[kinds.index("approval.granted") :]
    assert tail == [
        "approval.granted",
        "change.stage",
        "change.stage",
        "change.stage",
        "change.applied",
        "state.changed",
        "gate.verdict",
        "state.changed",
        "approval.requested",
    ]
    with psycopg.connect(pg["owner"], row_factory=dict_row) as c:
        g = c.execute(
            "SELECT channel, purpose, requested_by FROM gate_verdicts WHERE verdict_id = %s",
            (plan["last_gate"]["verdict_id"],),
        ).fetchone()
        req = c.execute("SELECT approval_requested_by FROM plans WHERE plan_id = %s", (pid,)).fetchone()
    assert (g["channel"], g["purpose"], g["requested_by"]) == ("system", "change_recheck", None)
    assert req["approval_requested_by"] == _principal(pg, "operator@bravo.test")  # the change's requester
    # The cutover approval binds the gate verdict; the approver is again a different person.
    aw = plan["awaiting_approval"]
    r = console.post(
        "approver",
        f"/plans/{pid}/approvals",
        {"stage": "cutover", "approval_request_id": aw["approval_request_id"], "revision": aw["revision"]},
    )
    assert r.status_code == 201 and r.json()["stage"] == "cutover"


def test_refused_apply_keeps_rehearse_with_the_reason_and_a_new_change_can_be_requested(
    console: Console, settings: Settings, pg: dict[str, str]
) -> None:
    pid, awaiting = _prepared_change(console, settings, pg)
    cid = awaiting["change"]["change_id"]
    _approve_change(console, pid, awaiting)
    _worker(pg, "UPDATE changes SET status = 'applying' WHERE change_id = %s", (cid,))
    # The DB itself refuses "applied" with a digest other than the approved one.
    with pytest.raises(psycopg.errors.CheckViolation, match="changes_applied_digest"):
        _worker(
            pg,
            "UPDATE changes SET status = 'applied', deployed_digest = %s WHERE change_id = %s",
            ("sha256:" + "ff" * 32, cid),
        )
    _stage(pg, cid, 2, "apply", "deploy.precheck", "REFUSED")
    _worker(
        pg,
        "UPDATE changes SET status = 'refused', reason = 'deployed_digest_mismatch', ended_at = now() WHERE change_id = %s",
        (cid,),
    )
    plan = _wait(
        lambda: (p := console.get("viewer", f"/plans/{pid}").json())["change"]["active"] is False and p, "refusal settled"
    )
    assert plan["state"] == "REHEARSE" and plan["state_reason"] == "change_refused:deployed_digest_mismatch"
    assert plan["awaiting_approval"] is None and plan["applied_image_digest"] is None
    assert "change.refused" in _kinds(pg, pid)
    # Only an operator, only on the console, opens a new change for the same passing rehearsal.
    assert console.post("viewer", f"/plans/{pid}/changes").status_code == 403
    r = console.post("operator", f"/plans/{pid}/changes")
    assert r.status_code == 201 and r.json()["status"] == "requested" and r.json()["change_id"] != cid
    again = console.post("operator", f"/plans/{pid}/changes")
    assert again.status_code == 409 and again.json()["error"] == "change_active"


def test_executor_role_reads_approvals_for_its_a2_check(console: Console, settings: Settings, pg: dict[str, str]) -> None:
    pid, awaiting = _prepared_change(console, settings, pg)
    a = _approve_change(console, pid, awaiting)
    with psycopg.connect(pg["bravo_executor"], row_factory=dict_row) as c:
        row = c.execute(
            "SELECT plan_id, stage, approver_id, requester_id, expires_at > now() AS live FROM approvals "
            "WHERE approval_id = %s",
            (a["approval_id"],),
        ).fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE approvals SET expires_at = now() WHERE approval_id = %s", (a["approval_id"],))
    assert str(row["plan_id"]) == pid and row["stage"] == "change" and row["live"] is True
    assert row["approver_id"] != row["requester_id"]


def test_unconfigured_collector_keeps_analyze_with_the_reason(
    keys: Keys, pg: dict[str, str], executor: ExecutorStub
) -> None:
    """Audit A4: no collector -> no inventory -> the plan does not skip ANALYZE."""
    executor.healthy()
    with TestClient(create_app(replace(make_settings(keys, pg, executor.url, Mode.API), collector_url=None))) as client:
        pid = Console(client, keys).plan()
        plan = Console(client, keys).get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "ANALYZE" and plan["state_reason"] == "collector_unconfigured"
    assert plan["analysis"]["inventory"]["status"] == "unconfigured" and plan["analysis"]["gaps"] == []
    assert plan["security_holds"] is None and plan["security_holds_reason"] == "not_implemented:D-20"


def test_secret_refs_resolve_in_openbao_or_block_collect(
    keys: Keys, pg: dict[str, str], executor: ExecutorStub, console: Console
) -> None:
    """Audit B11: an invented ref is refused; without OpenBao the environment is unverified and COLLECT holds."""
    r = console.post(
        "operator",
        "/environments",
        {"name": f"x-{uuid.uuid4().hex[:6]}", "role": "source", "secret_ref": "tenants/t1/invented"},
    )
    assert r.status_code == 422 and r.json()["error"] == "secret_ref_not_found"
    ok = console.post(
        "operator", "/environments", {"name": f"y-{uuid.uuid4().hex[:6]}", "role": "source", "secret_ref": "tenants/t1/src"}
    ).json()
    assert ok["secret_status"] == "resolved" and ok["secret_reason"] is None
    off = replace(make_settings(keys, pg, executor.url, Mode.API), bao_addr=None, bao_token_file=None)
    with TestClient(create_app(off)) as client:
        c = Console(client, keys)
        env = c.post(
            "operator",
            "/environments",
            {"name": f"z-{uuid.uuid4().hex[:6]}", "role": "source", "secret_ref": "tenants/t1/invented-too"},
        ).json()
        assert (env["secret_status"], env["secret_reason"]) == ("unverified", "openbao_not_connected")
        pid = c.plan()
        plan = c.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "COLLECT"
    assert plan["state_reason"] == "environment_secret_unverified:openbao_not_connected"
    # Once OpenBao is reachable again the same plan resolves its refs and moves on.
    r = console.post("operator", f"/plans/{pid}/gate")
    assert r.status_code == 200 and console.get("viewer", f"/plans/{pid}").json()["state"] != "COLLECT"


def test_watcher_labels_probe_denials_and_emits_each_trial_phase_once(
    console: Console, settings: Settings, pg: dict[str, str]
) -> None:
    """B10/P13: tool.denied carries origin (a probe's denial is not the agent's). B04/P2: trial.progress once per phase,
    even when two API replicas poll the same Trial (the events table is the dedupe record)."""
    from bravo_api.rehearsal import RehearsalWatcher

    pid = console.plan()
    rid = _to_rehearse(pg, pid, "operator@bravo.test")
    events = [{"seq": 1, "tool": "rehearsal_exec", "allowed": False, "rule": "free_text_command", "origin": "probe"},
              {"seq": 2, "tool": "rehearsal_exec", "allowed": False, "rule": "path_outside_workspace", "origin": "agent"},
              {"seq": 3, "tool": "repo_read", "allowed": True, "rule": "ok", "origin": "agent"}]
    body = {"trial_id": "t-test", "phase": "agent_running", "tool_events": events,
            "timings": {"elapsed_s": 14.2, "phase_s": {"provisioning": 14.2}},
            "tool_summary": {"agent": {"allowed": 1, "denied": 1}, "probe": {"allowed": 0, "denied": 1}}}

    async def go() -> None:
        db = Database(pg["bravo_api"])
        await db.open()
        try:
            for watcher in (RehearsalWatcher(db, None, settings), RehearsalWatcher(db, None, settings)):  # two replicas
                async with db.tenant_tx("t1") as conn:
                    await watcher._apply(conn, await _row(conn, rid), body)  # noqa: SLF001
        finally:
            await db.close()

    asyncio.run(go())
    with psycopg.connect(pg["owner"], row_factory=dict_row) as c:
        rows = c.execute("SELECT kind, data FROM events WHERE plan_id = %s AND kind IN ('tool.denied', 'trial.progress') "
                         "ORDER BY seq", (pid,)).fetchall()
    assert [(r["kind"], r["data"].get("origin"), r["data"].get("phase")) for r in rows] == [
        ("tool.denied", "probe", None), ("tool.denied", "agent", None), ("trial.progress", None, "agent_running")]
    assert rows[2]["data"]["elapsed_s"] == 14.2 and rows[2]["data"]["tool_summary"]["probe"]["denied"] == 1
