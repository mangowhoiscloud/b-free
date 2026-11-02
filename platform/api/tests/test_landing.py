"""Console landing for a beginner customer (2026-11-02): list plans and environments, JIT roles from the
Access identity via a config allowlist, and environments that reference the platform OpenBao layout."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import AccessMembers, Mode, access_members_from_file

from .conftest import CONSOLE_AUD, ExecutorStub, Keys, make_settings
from .test_api_flow import Console

OP, APPR, STRANGER = "jit-op@bravo.test", "jit-approver@bravo.test", "stranger@bravo.test"


@pytest.fixture(scope="module")
def api(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> Iterator[TestClient]:
    members = AccessMembers("t-jit", ((APPR, ("approver", "viewer")), (OP, ("operator", "viewer"))))
    with TestClient(create_app(replace(make_settings(keys, pg, executor.url, Mode.API), access_members=members))) as c:
        yield c


def _h(keys: Keys, email: str, sub: str) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": keys.token(email, CONSOLE_AUD, sub=sub), "Idempotency-Key": f"k-{uuid.uuid4().hex}"}


def test_access_allowlist_grants_listed_emails_once_and_nobody_else(api: TestClient, keys: Keys, pg: dict[str, str]) -> None:
    op_sub, appr_sub = f"op-{uuid.uuid4()}", f"ap-{uuid.uuid4()}"
    r = api.get("/v1/plans", headers=_h(keys, STRANGER, f"s-{uuid.uuid4()}"))
    assert r.status_code == 403 and r.json()["error"] == "unknown_principal"
    assert r.json()["detail"]["email"] == STRANGER
    assert api.get("/v1/plans", headers=_h(keys, OP, op_sub)).json() == {"plans": []}
    assert api.get("/v1/environments", headers=_h(keys, APPR, appr_sub)).status_code == 200
    with psycopg.connect(pg["owner"]) as c:
        roles = dict(
            c.execute(
                "SELECT p.email, array_agg(m.role ORDER BY m.role) FROM principals p "
                "JOIN memberships m ON m.principal_id = p.id "
                "WHERE m.tenant_id = 't-jit' GROUP BY p.email"
            ).fetchall()
        )
        assert roles == {OP: ["operator", "viewer"], APPR: ["approver", "viewer"]}
        # A later login of a listed email with a NEW identity (sub) is a first login for that identity only;
        # an identity that already has memberships never gets more from the allowlist.
        c.execute(
            "DELETE FROM memberships WHERE principal_id = (SELECT id FROM principals WHERE sub = %s) AND role = 'viewer'",
            (op_sub,),
        )
        c.commit()
    assert api.get("/v1/plans", headers=_h(keys, OP, op_sub)).status_code == 200
    with psycopg.connect(pg["owner"]) as c:
        n = c.execute(
            "SELECT count(*) FROM memberships m JOIN principals p ON p.id = m.principal_id WHERE p.sub = %s", (op_sub,)
        ).fetchone()[0]
    assert n == 1  # not re-granted


def test_allowlist_file_refuses_operator_plus_approver(tmp_path: Path) -> None:
    f = tmp_path / "members.json"
    f.write_text(json.dumps({"tenant": "t", "members": [{"email": "a@b.c", "roles": ["operator", "approver"]}]}))
    with pytest.raises(RuntimeError, match="separation of duties"):
        access_members_from_file(str(f))
    f.write_text(json.dumps({"tenant": "t", "members": [{"email": "a@b.c", "roles": ["operator"]}]}))
    assert access_members_from_file(str(f)).roles_for("A@B.C") == ("operator",)  # type: ignore[union-attr]
    real = Path(__file__).resolve().parents[3] / "deploy" / "platform" / "access" / "members.json"
    cfg = access_members_from_file(str(real))
    assert cfg is not None and cfg.roles_for("ryoo0504@naver.com") == ("operator", "viewer")
    assert cfg.roles_for("ryoo0504@gmail.com") == ("approver", "viewer") and cfg.roles_for("x@y.z") is None


def test_landing_lists_plans_and_environments_with_real_status(
    keys: Keys, pg: dict[str, str], executor: ExecutorStub
) -> None:
    executor.healthy()
    with TestClient(create_app(make_settings(keys, pg, executor.url, Mode.API))) as client:
        console = Console(client, keys)
        tag = uuid.uuid4().hex[:6]
        # The platform OpenBao layout: envs/<this environment's name>/<secret>, resolved by metadata.
        bad = console.post(
            "operator", "/environments", {"name": f"x-{tag}", "role": "source", "secret_ref": "envs/landing-src/chat-db"}
        )
        assert bad.status_code == 422 and bad.json()["error"] == "secret_ref_out_of_scope"
        names = {}
        for name, role in (("landing-src", "source"), ("landing-dst", "target")):
            r = console.post("operator", "/environments", {"name": name, "role": role, "secret_ref": f"envs/{name}/chat-db"})
            if r.status_code == 409:  # module re-run against the session DB
                continue
            assert r.status_code == 201 and r.json()["secret_status"] == "resolved", r.text
            names[name] = r.json()["environment_id"]
        envs = console.get("viewer", "/environments").json()
        by_name = {e["name"]: e for e in envs["environments"]}
        src = by_name["landing-src"]
        assert src["secret_status"] == "resolved" and src["secret_ref"] == "envs/landing-src/chat-db"
        # The test collector's registry has no clusters: said as such, not invented.
        assert src["cluster"] == {"status": "unknown", "reason": "not_in_collector_registry"}
        assert envs["cluster_source"]["status"] == "ok"
        r = console.post(
            "operator",
            "/plans",
            {
                "workload": "chat",
                "source_environment_id": src["environment_id"],
                "target_environment_id": by_name["landing-dst"]["environment_id"],
            },
        )
        assert r.status_code == 201
        pid = r.json()["plan_id"]
        console.post("operator", f"/plans/{pid}/gate")
        plans = console.get("viewer", "/plans").json()["plans"]
        mine = next(p for p in plans if p["plan_id"] == pid)
        assert mine["source"]["name"] == "landing-src" and mine["target"]["name"] == "landing-dst"
        assert mine["state"] == "AWAIT_APPROVAL" and mine["pending_approval"]["stage"] == "cutover"
        assert mine["console_url"].endswith(f"/plans/{pid}")
        assert console.get("viewer", "/plans?limit=0").status_code == 422
