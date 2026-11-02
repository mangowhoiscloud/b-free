"""The worker against the real migrations (platform/api 0001..0004): what role bravo_apply can and cannot do.

Seeded as the DB owner with plain SQL. The approval row goes through the real ``bravo_approvals_bind`` trigger,
and the move to ``approved`` is made as role ``bravo_api``, as platform/api does.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from bravo_apply import config, digests
from bravo_apply.worker import Worker

pytestmark = pytest.mark.pg
DIFF = "--- a/src/bravo_demo/config.py\n+++ b/src/bravo_demo/config.py\n@@ -1 +1 @@\n-A = 1\n+A = 2\n"
ARTIFACT = {"role": "app_migration", "step": "fix", "diff": DIFF, "files": ["src/bravo_demo/config.py"]}
IMG = "sha256:" + "ab" * 32


def _seed(owner: str) -> dict[str, uuid.UUID]:
    ids = {k: uuid.uuid4() for k in ("op", "req", "appr", "src", "dst", "plan", "reh", "chg")}
    tenant = "t" + uuid.uuid4().hex[:8]
    ids["tenant"] = tenant  # type: ignore[assignment]
    with psycopg.connect(owner, autocommit=True) as c, c.transaction():
        c.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, 'apply test')", (tenant,))
        for k in ("op", "req", "appr"):
            c.execute("INSERT INTO principals (id, iss, sub, email) VALUES (%s, 'https://iss.test', %s, %s)",
                      (ids[k], f"{k}-{ids[k]}", f"{k}@bravo.test"))
        for k, role, name in (("src", "source", "source-aws"), ("dst", "target", "target-onprem")):
            c.execute("""INSERT INTO environments (environment_id, tenant_id, name, role, engine, secret_ref, created_by)
                         VALUES (%s, %s, %s, %s, 'postgres', %s, %s)""",
                      (ids[k], tenant, name, role, f"tenants/{tenant}/{name}", ids["op"]))
        c.execute("""INSERT INTO plans (plan_id, tenant_id, workload, source_environment_id, target_environment_id, state, created_by)
                     VALUES (%s, %s, 'chat', %s, %s, 'REHEARSE', %s)""", (ids["plan"], tenant, ids["src"], ids["dst"], ids["op"]))
        c.execute("""INSERT INTO rehearsals (rehearsal_id, tenant_id, plan_id, attempt, task, role, idem_key, status,
                                             requested_by, channel, artifact, artifact_digest)
                     VALUES (%s, %s, %s, 1, 'fix-secrets-manager', 'app_migration', %s, 'PASS', %s, 'console', %s, %s)""",
                  (ids["reh"], tenant, ids["plan"], f"rehearsal-{ids['reh']}", ids["req"], Jsonb(ARTIFACT),
                   digests.artifact_digest(ARTIFACT)))
        bv = digests.bound_values(ARTIFACT)
        c.execute("""INSERT INTO changes (change_id, tenant_id, plan_id, rehearsal_id, plan_revision, target_environment_id, role,
                                          artifact_digest, diff_digest, policy_diff_digest, policy_targets, status, requested_by)
                     VALUES (%s, %s, %s, %s, 2, %s, 'app_migration', %s, %s, %s, %s, 'requested', %s)""",
                  (ids["chg"], tenant, ids["plan"], ids["reh"], ids["dst"], digests.artifact_digest(ARTIFACT),
                   bv["diff_digest"], bv["policy_diff_digest"], Jsonb(bv["policy_targets"]), ids["req"]))
        c.execute("UPDATE plans SET revision = 2, active_change_id = %s WHERE plan_id = %s", (ids["chg"], ids["plan"]))
    return ids


def _apply_conn(pg: dict[str, str]) -> psycopg.Connection:
    return psycopg.connect(pg["bravo_apply"], autocommit=True, row_factory=dict_row)


def _status(pg: dict[str, str], chg: uuid.UUID) -> str:
    with _apply_conn(pg) as c:
        return c.execute("SELECT status FROM changes WHERE change_id = %s", (chg,)).fetchone()["status"]


def _violation(pg: dict[str, str], sql: str, params: tuple) -> str:
    with _apply_conn(pg) as c, pytest.raises(psycopg.errors.CheckViolation) as e:
        c.execute(sql, params)
    return str(e.value.diag.message_primary or e.value.diag.constraint_name)


def test_worker_prepare_path_and_what_it_cannot_change(pg: dict[str, str], tmp_path: Path, monkeypatch) -> None:
    ids = _seed(pg["owner"])
    chg = ids["chg"]
    # A worker whose tool pins do not match fails closed: prepare_failed, one stage row, nothing built.
    bad_lock = tmp_path / "lock.json"
    bad_lock.write_text(json.dumps({"tools": {n: {"version": "0", "path": str(tmp_path / n), "sha256": "0" * 64,
                                                  "version_cmd": ["--version"]} for n in ("docker-buildx", "syft", "trivy")}}))
    monkeypatch.setenv("BRAVO_APPLY_TOOLS_LOCK", str(bad_lock))
    wk = Worker(config.load(), pg["bravo_apply"], tmp_path / "evidence", worker_id="test-worker")
    assert wk.run_once() == 1
    with _apply_conn(pg) as c:
        row = c.execute("SELECT status, reason, worker FROM changes WHERE change_id = %s", (chg,)).fetchone()
        stages = c.execute("SELECT seq, phase, name, result, detail FROM change_stages WHERE change_id = %s ORDER BY seq",
                           (chg,)).fetchall()
    assert (row["status"], row["reason"], row["worker"]) == ("prepare_failed", "tool_pin_mismatch", "test-worker")
    assert [(s["seq"], s["phase"], s["name"], s["result"]) for s in stages] == [(1, "prepare", "tools.verify", "REFUSED")]
    # prepare_failed is terminal for the worker.
    assert "change_status_not_allowed" in _violation(pg, "UPDATE changes SET status = 'preparing' WHERE change_id = %s", (chg,))


def test_guard_allows_only_worker_transitions_and_freezes_bound_values(pg: dict[str, str]) -> None:
    ids = _seed(pg["owner"])
    chg, plan = ids["chg"], ids["plan"]
    with _apply_conn(pg) as c:
        c.execute("UPDATE changes SET status = 'preparing', worker = 'w' WHERE change_id = %s", (chg,))
    # Bound values are the API's: the worker can never rewrite them.
    for col, val in (("diff_digest", "sha256:" + "0" * 64), ("artifact_digest", "sha256:" + "1" * 64),
                     ("policy_targets", Jsonb([{"type": "k8s_rbac", "name": "x"}]))):
        with _apply_conn(pg) as c, pytest.raises(psycopg.Error):  # trigger (check_violation) or missing column grant
            c.execute(f"UPDATE changes SET {col} = %s WHERE change_id = %s", (val, chg))
    with _apply_conn(pg) as c:
        c.execute("""UPDATE changes SET status = 'prepared', image_repository = 'bravo.local/bravo-demo', image_digest = %s,
                     prepared_at = now() WHERE change_id = %s""", (IMG, chg))
    # After prepare the candidate is frozen, and only the API may approve.
    assert "change_candidate_immutable" in _violation(
        pg, "UPDATE changes SET image_digest = %s WHERE change_id = %s", ("sha256:" + "cd" * 32, chg))
    assert "change_status_not_allowed" in _violation(pg, "UPDATE changes SET status = 'approved' WHERE change_id = %s", (chg,))
    assert _status(pg, chg) == "prepared"
    with _apply_conn(pg) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE plans SET state = 'GATE' WHERE plan_id = %s", (plan,))

    # The API's side: change approval request, approval row (real bind trigger), prepared -> approved as bravo_api.
    req, exp = uuid.uuid4(), datetime.now(UTC) + timedelta(minutes=15)
    bv = digests.bound_values(ARTIFACT)
    with psycopg.connect(pg["owner"], autocommit=True) as c, c.transaction():
        c.execute("""UPDATE plans SET approval_stage = 'change', approval_request_id = %s, approval_request_expires_at = %s,
                     approval_requested_by = %s WHERE plan_id = %s""", (req, exp, ids["req"], plan))
        c.execute("UPDATE changes SET approval_request_id = %s WHERE change_id = %s", (req, chg))
        approval = uuid.uuid4()
        c.execute("""INSERT INTO approvals (approval_id, tenant_id, plan_id, approval_request_id, stage, plan_revision,
                                            approver_id, proposer_id, requester_id, expires_at, artifact_digest, change_id,
                                            diff_digest, image_digest, policy_diff_digest)
                     VALUES (%s, %s, %s, %s, 'change', 2, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                  (approval, ids["tenant"], plan, req, ids["appr"], ids["appr"], ids["appr"], exp,
                   digests.artifact_digest(ARTIFACT), chg, bv["diff_digest"], IMG, bv["policy_diff_digest"]))
    with psycopg.connect(pg["bravo_api"]) as c, c.transaction():
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (ids["tenant"],))
        c.execute("UPDATE changes SET status = 'approved', approval_id = %s, approved_at = now() WHERE change_id = %s",
                  (approval, chg))
    assert _status(pg, chg) == "approved"

    # Worker apply path: it cannot touch the approval link, and "applied" needs deployed digest == approved digest.
    assert "change_approval_owned_by_api" in _violation(
        pg, "UPDATE changes SET approval_id = %s WHERE change_id = %s", (uuid.uuid4(), chg))
    with _apply_conn(pg) as c:
        c.execute("UPDATE changes SET status = 'applying' WHERE change_id = %s", (chg,))
        a = c.execute("SELECT image_digest, policy_diff_digest FROM approvals WHERE approval_id = %s", (approval,)).fetchone()
    assert a["image_digest"] == IMG
    assert "changes_applied_digest" in _violation(
        pg, "UPDATE changes SET status = 'applied', deployed_digest = %s WHERE change_id = %s", ("sha256:" + "ef" * 32, chg))
    with _apply_conn(pg) as c:
        c.execute("UPDATE changes SET status = 'applied', deployed_digest = %s, ended_at = now() WHERE change_id = %s", (IMG, chg))
    assert _status(pg, chg) == "applied"
    assert "change_status_not_allowed" in _violation(pg, "UPDATE changes SET status = 'applying' WHERE change_id = %s", (chg,))
