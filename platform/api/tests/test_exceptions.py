"""Control exception (spec S1 exception flow 5b, contract 15 §9) through the running API and a real postgres:16.

The gate service is an HTTP stub serving ``/v1/controls/report`` in platform/gate's shape (question rows with
``dst_evidence_digest``, see platform/gate ``build_rows``); the Executor stub and everything else are the same
as the other API tests. Covered: separation of duties (API and DB trigger), UNKNOWN not exceptable (API and DB
trigger), parity excludes EXCEPTION, stale on evidence change.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode

from .conftest import ExecutorStub, Keys, _serve, make_settings
from .test_api_flow import Console

D1 = "sha256:" + "11" * 32
D2 = "sha256:" + "22" * 32
SRC_DIGEST = "sha256:" + "0a" * 32


@dataclass
class GateStub:
    url: str = ""
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    ttl: timedelta = timedelta(hours=1)  # report expires_at = now + ttl (BRAVO_EVIDENCE_TTL_S on the gate)


@pytest.fixture(scope="module")
def gate() -> GateStub:
    app = FastAPI()
    stub = GateStub()

    @app.get("/v1/controls/report")
    def report(source: str, target: str) -> dict[str, Any]:
        stub.calls.append((source, target))
        questions = [{"control_id": cid, "question": "q", "src_evidence_digest": SRC_DIGEST, **row}
                     for cid, row in stub.rows.items()]
        return {"rule_version": "gate-controls/2026-11-02.1", "source": source, "target": target,
                "questions": questions, "expires_at": (datetime.now(UTC) + stub.ttl).isoformat()}

    stub.url = _serve(app)
    return stub


@pytest.fixture(scope="module")
def api(keys: Keys, pg: dict[str, str], executor: ExecutorStub, gate: GateStub) -> Iterator[TestClient]:
    settings = replace(make_settings(keys, pg, executor.url, Mode.API), gate_controls_url=gate.url)
    with TestClient(create_app(settings)) as client:
        yield client


def _principal(pg: dict[str, str], mail: str) -> str:
    with psycopg.connect(pg["owner"]) as c:
        return str(c.execute("SELECT id FROM principals WHERE email = %s", (mail,)).fetchone()[0])


def _as_api(pg: dict[str, str], principal_id: str) -> psycopg.Connection:
    c = psycopg.connect(pg["bravo_api"])
    c.execute("SELECT set_config('app.tenant_id', 't1', false), set_config('app.principal_id', %s, false)",
              (principal_id,))
    return c


def test_exception_flow_s1_5b(api: TestClient, keys: Keys, executor: ExecutorStub, gate: GateStub,
                              pg: dict[str, str]) -> None:
    executor.healthy()  # DB-ANON-READ, DB-AUTH PASS/PASS, ready (no evidence digest: not exceptable)
    gate.rows = {
        # Reported by the Executor too, with the same values: merged (contract 15 §9.5), both sources kept.
        "DB-AUTH": {"src": "PASS", "dst": "PASS", "dst_evidence_digest": "sha256:" + "a0" * 32},
        "OBJ-05": {"src": "PASS", "dst": "UNKNOWN", "dst_evidence_digest": "sha256:" + "55" * 32},
        "OBJ-06": {"src": "PASS", "dst": "FAIL", "dst_evidence_digest": D1},
    }
    console = Console(api, keys)
    pid = console.plan()
    assert console.get("viewer", f"/plans/{pid}").json()["state"] == "GATE"
    assert gate.calls and gate.calls[-1][0].startswith("src-") and gate.calls[-1][1].startswith("dst-")

    g1 = console.post("operator", f"/plans/{pid}/gate").json()
    assert g1["verdict"] == "BLOCK" and g1["reasons"] == ["OBJ-05:unknown", "OBJ-06:regression"]
    row = next(c for c in g1["controls"] if c["control_id"] == "OBJ-06")
    assert row["evidence_digest"] == D1 and row["exception_id"] is None
    auth = next(c for c in g1["controls"] if c["control_id"] == "DB-AUTH")
    assert (auth["verdict"], [x["source"] for x in auth["sources"]]) == ("PASS", ["executor", "gate"])
    expires = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    body = {"control_id": "OBJ-06", "gate_verdict_id": g1["verdict_id"], "evidence_digest": D1,
            "reason": "GCS XML API accepts plain HTTP; no organization for storage.secureHttpTransport",
            "compensating_control": "node egress firewall denies port 80; app NetworkPolicy allows 443 only",
            "expires_at": expires}

    # 5a: UNKNOWN is never exceptable. API first, then the DB trigger on a direct insert.
    r = console.post("operator", f"/plans/{pid}/exceptions", {**body, "control_id": "OBJ-05",
                                                             "evidence_digest": "sha256:" + "55" * 32})
    assert r.status_code == 422 and r.json() == {"error": "exception_not_allowed", "detail": {"cause": "unknown"}}
    r = console.post("operator", f"/plans/{pid}/exceptions", {**body, "control_id": "DB-AUTH"})
    assert r.status_code == 422 and r.json()["detail"]["cause"] == "not_failed"
    plan_row = console.get("viewer", f"/plans/{pid}").json()
    operator_id = _principal(pg, "operator@bravo.test")
    with _as_api(pg, operator_id) as c, pytest.raises(psycopg.errors.CheckViolation, match="exception_stale"):
        c.execute(
            """INSERT INTO control_exceptions (exception_id, tenant_id, plan_id, control_id, target_environment_id,
                                               gate_verdict_id, src, dst, evidence_digest, reason, expires_at,
                                               requested_by)
               VALUES (%s, 't1', %s, 'OBJ-05', %s, %s, 'PASS', 'FAIL', %s, 'unknown is not a fail', %s, %s)""",
            (uuid.uuid4(), pid, plan_row["target_environment_id"], g1["verdict_id"], "sha256:" + "55" * 32, expires,
             operator_id),
        )
    r = console.post("operator", f"/plans/{pid}/exceptions", {**body, "evidence_digest": D2})
    assert r.status_code == 409 and r.json()["detail"]["cause"] == "evidence_changed"
    r = console.post("operator", f"/plans/{pid}/exceptions",
                     {**body, "expires_at": (datetime.now(UTC) + timedelta(days=40)).isoformat()})
    assert r.status_code == 422 and r.json()["error"] == "exception_expiry_invalid"

    # Request by "dual" (holds operator and approver): it can never decide its own request.
    r = console.post("dual", f"/plans/{pid}/exceptions", body)
    assert r.status_code == 201, r.text
    ex = r.json()
    assert (ex["status"], ex["src"], ex["dst"], ex["evidence_digest"]) == ("requested", "PASS", "FAIL", D1)
    assert ex["requested_by"] == "dual@bravo.test" and ex["target_environment"]["name"].startswith("dst-")
    assert console.post("operator", f"/plans/{pid}/exceptions", body).json()["error"] == "exception_open"
    approve = f"/plans/{pid}/exceptions/{ex['exception_id']}/approve"
    assert console.post("operator", approve, {"evidence_digest": D1}).json()["error"] == "insufficient_scope"
    r = console.post("dual", approve, {"evidence_digest": D1})
    assert r.status_code == 403 and r.json()["error"] == "separation_of_duties"
    dual_id = _principal(pg, "dual@bravo.test")
    with _as_api(pg, dual_id) as c, pytest.raises(psycopg.errors.CheckViolation, match="separation_of_duties"):
        c.execute("UPDATE control_exceptions SET status = 'approved', decided_by = %s WHERE exception_id = %s",
                  (dual_id, ex["exception_id"]))
    r = console.post("approver", approve, {"evidence_digest": D2})
    assert r.status_code == 409 and r.json()["detail"]["cause"] == "evidence_mismatch"
    r = console.post("approver", approve, {"evidence_digest": D1, "approver": "operator@bravo.test"})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["decided_by"], r.json()["applied"]) == ("approved", "approver@bravo.test", False)

    # The next verdict shows OBJ-06 as an approved exception, apart from parity. UNKNOWN still blocks (5a).
    g2 = console.post("operator", f"/plans/{pid}/gate").json()
    assert g2["verdict"] == "BLOCK" and g2["reasons"] == ["OBJ-05:unknown"]
    row = next(c for c in g2["controls"] if c["control_id"] == "OBJ-06")
    assert (row["verdict"], row["reason"], row["exception_id"]) == ("EXCEPTION", "exception_approved",
                                                                    ex["exception_id"])
    assert [(e["exception_id"], e["approved_by"], e["compensating_control"]) for e in g2["exceptions"]] == [
        (ex["exception_id"], "approver@bravo.test", body["compensating_control"])]
    assert g2["evidence_expires_at"] is not None

    # Everything else passes -> PASS: the excepted control is left out of the parity verdict.
    gate.rows["OBJ-05"] = {"src": "PASS", "dst": "PASS", "dst_evidence_digest": "sha256:" + "56" * 32}
    g3 = console.post("operator", f"/plans/{pid}/gate").json()
    assert g3["verdict"] == "PASS" and g3["reasons"] == [] and g3["state"] == "AWAIT_APPROVAL"
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["last_gate"]["exceptions"][0]["exception_id"] == ex["exception_id"]
    listed = console.get("viewer", f"/plans/{pid}/exceptions").json()["exceptions"]
    assert [(e["status"], e["applied"]) for e in listed] == [("approved", True)]

    # The destination evidence changes -> the exception is stale and the regression blocks again.
    gate.rows["OBJ-06"] = {"src": "PASS", "dst": "FAIL", "dst_evidence_digest": D2}
    g4 = console.post("operator", f"/plans/{pid}/gate").json()
    assert g4["verdict"] == "BLOCK" and g4["reasons"] == ["OBJ-06:regression"] and g4["exceptions"] == []
    assert g4["state"] == "GATE"
    listed = console.get("viewer", f"/plans/{pid}/exceptions").json()["exceptions"]
    assert [(e["status"], e["stale_reason"], e["applied"]) for e in listed] == [("stale", "evidence_changed", False)]
    r = console.post("approver", approve, {"evidence_digest": D1})
    assert r.status_code == 409 and r.json() == {"error": "exception_not_pending", "detail": {"status": "stale"}}
    with psycopg.connect(pg["owner"]) as c:
        kinds = [k for (k,) in c.execute("SELECT kind FROM events WHERE plan_id = %s AND kind LIKE 'exception.%%' "
                                         "ORDER BY seq", (pid,))]
        stale = c.execute("SELECT data FROM events WHERE plan_id = %s AND kind = 'exception.stale'", (pid,)).fetchone()
    assert kinds == ["exception.requested", "exception.approved", "exception.stale"]
    assert stale[0]["cause"] == "evidence_changed" and stale[0]["was"]["evidence_digest"] == D1
    assert stale[0]["now"]["evidence_digest"] == D2 and stale[0]["verdict_id"] == g4["verdict_id"]

    # A fresh request on the new evidence can be rejected by the approver.
    r = console.post("operator", f"/plans/{pid}/exceptions",
                     {**body, "gate_verdict_id": g4["verdict_id"], "evidence_digest": D2})
    assert r.status_code == 201, r.text
    r = console.post("approver", f"/plans/{pid}/exceptions/{r.json()['exception_id']}/reject", {"note": "fix it"})
    assert (r.status_code, r.json()["status"], r.json()["decision_note"]) == (200, "rejected", "fix it")
    r = console.post("operator", f"/plans/{pid}/exceptions", {**body, "gate_verdict_id": g1["verdict_id"]})
    assert r.status_code == 409 and r.json()["detail"]["cause"] == "gate_verdict_superseded"

    # Spec §8.5: expired evidence is never used for an exception.
    gate.ttl = timedelta(seconds=-1)
    g5 = console.post("operator", f"/plans/{pid}/gate").json()
    r = console.post("operator", f"/plans/{pid}/exceptions",
                     {**body, "gate_verdict_id": g5["verdict_id"], "evidence_digest": D2})
    assert r.status_code == 409 and r.json()["detail"]["cause"] == "evidence_expired"
    gate.ttl = timedelta(hours=1)
