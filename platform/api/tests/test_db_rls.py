"""Tenant isolation and role privileges in the real database (08 5, 9)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors


def test_service_roles_have_no_bypassrls(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["owner"]) as c:
        rows = c.execute("SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname LIKE 'bravo\\_%'").fetchall()
    assert {r[0] for r in rows} >= {"bravo_api", "bravo_mcp", "bravo_executor"}
    assert not any(r[1] or r[2] for r in rows if r[0] != "bravo_owner")


def test_force_rls_on_every_table(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["owner"]) as c:
        rows = c.execute("""SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
                             WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
                               AND relname <> 'schema_migrations'""").fetchall()
    assert rows and all(r[1] and r[2] for r in rows), rows


@pytest.mark.parametrize("tenant", [None, "", "t2"])
def test_other_or_missing_tenant_sees_nothing(pg: dict[str, str], tenant: str | None) -> None:
    with psycopg.connect(pg["bravo_api"]) as c:
        if tenant is not None:
            c.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
        for table in ("plans", "environments", "events", "approvals", "gate_verdicts", "runs"):
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_tenant_cannot_insert_into_other_tenant(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["bravo_api"]) as c:
        c.execute("SELECT set_config('app.tenant_id', 't2', true)")
        pid = c.execute("SELECT id FROM principals LIMIT 1").fetchone()
        assert pid is None  # t2 has no members, so principals are hidden too
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO environments (environment_id, tenant_id, name, role, engine, secret_ref, created_by) "
                "VALUES (gen_random_uuid(), 't1', 'evil', 'source', 'postgres', 'tenants/t1/x', gen_random_uuid())"
            )


def test_mcp_role_cannot_write_approvals_or_execution_columns(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["bravo_mcp"]) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("INSERT INTO approvals (approval_id) VALUES (gen_random_uuid())")
    with psycopg.connect(pg["bravo_mcp"]) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("UPDATE plans SET epoch = epoch + 1, active_run_id = gen_random_uuid()")
    with psycopg.connect(pg["bravo_mcp"]) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO runs (run_id, plan_id, kind, epoch, idem_key, status) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), 'cutover', 1, 'x', 'RUNNING')"
            )


def test_mcp_role_idempotency_limited_to_mcp_actions(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["bravo_mcp"]) as c:
        c.execute("SELECT set_config('app.tenant_id', 't1', true)")
        pid = c.execute("SELECT id FROM principals WHERE email = 'operator@bravo.test'").fetchone()[0]
        with pytest.raises(errors.InsufficientPrivilege):  # RLS WITH CHECK violation
            c.execute(
                "INSERT INTO idempotency (tenant_id, action, idem_key, request_digest, principal_id) "
                "VALUES ('t1', 'shift', 'key-12345678', 'd', %s)",
                (pid,),
            )


def test_executor_role_limited_to_contract_tables(pg: dict[str, str]) -> None:
    with psycopg.connect(pg["bravo_executor"]) as c:
        with pytest.raises(errors.InsufficientPrivilege):
            c.execute("SELECT * FROM plans")
